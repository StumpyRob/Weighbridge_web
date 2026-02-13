from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models.base import utcnow
from ..models import (
    Area,
    Container,
    Customer,
    DirectionEnum,
    Destination,
    Driver,
    EwcCode,
    Haulier,
    Invoice,
    Product,
    Ticket,
    TicketVoid,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
    WasteProducerSourceEnum,
    VoidReason,
    Yard,
)
from ..security import validate_no_html, validate_no_html_fields
from ..seed import seed_void_reasons
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

LOCKED_STATUSES = {TicketStatusEnum.COMPLETE.value, TicketStatusEnum.VOID.value}
NEW_TICKET_DEDUP_SECONDS = 5
WEIGHT_MAX_KG = Decimal("1000000")
WEIGHT_QUANTIZE = Decimal("1")
INACTIVE_PRODUCT_UNIT_ERROR = "Product unit is inactive. Choose a different product."
SALES_ONLY_WASTE_ERROR = (
    "This product is sales-only and cannot be used on waste tickets."
)
SALES_ONLY_WASTE_WARNING = (
    "Selected product is sales-only and cannot be used on waste tickets."
)


@router.get("/tickets", response_class=HTMLResponse)
def tickets_list(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    open_only: int | None = None,
    direction: str | None = None,
    transaction_type: str | None = None,
    ticket_no: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    filters = []
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    if open_only:
        filters.append(Ticket.status == TicketStatusEnum.OPEN.value)
    elif status:
        filters.append(Ticket.status == status)
    if direction:
        filters.append(Ticket.direction == direction)
    if transaction_type:
        filters.append(Ticket.transaction_type == transaction_type)
    if q:
        like = f"%{q.lower()}%"
        filters.append(
            or_(
                func.lower(Ticket.ticket_no).like(like),
                func.lower(Vehicle.registration).like(like),
            )
        )
    elif ticket_no:
        ticket_like = f"%{ticket_no.lower()}%"
        filters.append(func.lower(Ticket.ticket_no).like(ticket_like))

    base_stmt = (
        select(Ticket, Vehicle)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .where(*filters)
    )
    count_stmt = (
        select(func.count(func.distinct(Ticket.id)))
        .select_from(Ticket)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .where(*filters)
    )
    total_count = db.execute(count_stmt).scalar() or 0
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page = min(page, total_pages)

    status_priority = case(
        (Ticket.status == TicketStatusEnum.OPEN.value, 0),
        (Ticket.status == TicketStatusEnum.COMPLETE.value, 1),
        (Ticket.status == TicketStatusEnum.VOID.value, 2),
        else_=3,
    )
    rows = (
        db.execute(
            base_stmt.order_by(Ticket.datetime.desc(), status_priority.asc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        .all()
    )

    return templates.TemplateResponse(request, 
        "tickets/list.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
            "filters": {
                "date_from": date_from.isoformat() if date_from else "",
                "date_to": date_to.isoformat() if date_to else "",
                "status": status or "",
                "open_only": "1" if open_only else "",
                "direction": direction or "",
                "transaction_type": transaction_type or "",
                "ticket_no": ticket_no or "",
                "q": q or "",
            },
        },
    )


@router.post("/tickets/new/quick", response_class=HTMLResponse)
def tickets_quick_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    now = utcnow()
    recent_cutoff = now - timedelta(seconds=NEW_TICKET_DEDUP_SECONDS)
    recent_ticket = (
        db.execute(
            select(Ticket)
            .where(
                Ticket.status == TicketStatusEnum.OPEN.value,
                Ticket.created_at >= recent_cutoff,
                Ticket.updated_at == Ticket.created_at,
                Ticket.direction == DirectionEnum.INWARD.value,
                Ticket.transaction_type == TransactionTypeEnum.WASTEIN.value,
                Ticket.customer_id.is_(None),
                Ticket.vehicle_id.is_(None),
                Ticket.product_id.is_(None),
                Ticket.haulier_id.is_(None),
                Ticket.driver_id.is_(None),
                Ticket.container_id.is_(None),
                Ticket.destination_id.is_(None),
                Ticket.gross_kg.is_(None),
                Ticket.tare_kg.is_(None),
                Ticket.net_kg.is_(None),
                Ticket.qty.is_(None),
                Ticket.unit_price.is_(None),
                Ticket.total.is_(None),
                Ticket.dont_invoice.is_(False),
                Ticket.paid.is_(False),
            )
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if recent_ticket:
        return RedirectResponse(url=f"/tickets/{recent_ticket.id}", status_code=303)
    ticket = Ticket(
        ticket_no=_generate_ticket_no(db, now),
        datetime=now.replace(second=0, microsecond=0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        waste_producer_source=WasteProducerSourceEnum.CUSTOMER.value,
        dont_invoice=False,
        paid=False,
    )
    db.add(ticket)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket.id}", status_code=303)


@router.get("/tickets/product-defaults", response_class=HTMLResponse)
def ticket_product_defaults(
    request: Request,
    product_id: str | None = Query(None),
    ticket_id: str | None = Query(None),
    qty: str | None = Query(None),
    gross_kg: str | None = Query(None),
    tare_kg: str | None = Query(None),
    net_kg: str | None = Query(None),
    readout_kg: str | None = Query(None),
    unit_price: str | None = Query(None),
    transaction_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product_id_values = request.query_params.getlist("product_id")
    parsed_product_id = None
    for raw in product_id_values:
        candidate = _parse_int(str(raw).strip())
        if candidate is not None:
            parsed_product_id = candidate
            break
    if parsed_product_id is None and product_id is not None:
        parsed_product_id = _parse_int(str(product_id).strip())

    parsed_ticket_id = _parse_int(str(ticket_id).strip()) if ticket_id else None

    if not parsed_product_id:
        return HTMLResponse("", status_code=204)

    product = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit))
            .where(Product.id == parsed_product_id)
        )
        .scalars()
        .first()
    )
    if not product:
        return HTMLResponse("", status_code=204)
    if not _product_has_active_unit(product):
        return templates.TemplateResponse(
            request,
            "tickets/_product_defaults_error.html",
            {"request": request, "error": INACTIVE_PRODUCT_UNIT_ERROR},
            status_code=400,
        )
    if _is_waste_transaction((transaction_type or "").strip().upper()) and product.sales_only:
        return templates.TemplateResponse(
            request,
            "tickets/_product_defaults_error.html",
            {"request": request, "error": SALES_ONLY_WASTE_ERROR},
            status_code=400,
        )

    # Product switch always resets rate to the selected product's canonical default.
    unit_price_display = (
        f"{product.unit_price:.2f}" if product.unit_price is not None else ""
    )

    ewc = product.ewc_code if product else None
    unit_name = product.unit.name if product and product.unit else ""
    unit_type = product.unit.unit_type if product and product.unit else ""
    resolved_unit_type = (unit_type or "").upper()
    qty_value = qty.strip() if qty is not None else ""
    if resolved_unit_type == "WEIGHT":
        qty_value = ""

    weights_ticket = db.get(Ticket, parsed_ticket_id) if parsed_ticket_id else None
    weights_form = None
    weights_is_open = False
    if weights_ticket is not None:
        gross_raw = (gross_kg or "").strip()
        tare_raw = (tare_kg or "").strip()
        readout_raw = (readout_kg or "").strip()
        gross_value = _parse_float(gross_raw)
        tare_value = _parse_float(tare_raw)
        net_value = _parse_float((net_kg or "").strip())
        if net_value is None and gross_value is not None and tare_value is not None:
            net_value = gross_value - tare_value
        weights_form = _weights_form_from_values(
            ticket=weights_ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            readout_raw=readout_raw,
            net_value=net_value,
        )
        weights_form["product_id"] = str(parsed_product_id or weights_ticket.product_id or "")
        weights_is_open = _is_open_ticket(weights_ticket)

    return templates.TemplateResponse(request, 
        "tickets/_product_defaults.html",
        {
            "request": request,
            "ewc_code_6": ewc.code_6 if ewc else None,
            "unit_price": unit_price_display,
            "ewc_code_display": ewc.code_display if ewc else None,
            "ewc_description": ewc.description if ewc else None,
            "ewc_hazardous": bool(ewc.hazardous) if ewc else False,
            "default_destination_id": product.default_destination_id,
            "unit_type": unit_type,
            "unit_name": unit_name,
            "transaction_type": transaction_type or "",
            "resolved_unit_type": resolved_unit_type,
            "qty_value": qty_value,
            "oob_qty": True,
            "weights_ticket": weights_ticket,
            "weights_form": weights_form,
            "weights_is_open": weights_is_open,
            "weights_locked": resolved_unit_type == "COUNT",
        },
    )


@router.get("/tickets/mismatch-warning", response_class=HTMLResponse)
def tickets_mismatch_warning(
    request: Request,
    direction: str | None = Query(None),
    transaction_type: str | None = Query(None),
) -> HTMLResponse:
    warning = _direction_transaction_warning(direction, transaction_type)
    if not warning:
        return HTMLResponse("", status_code=200)
    return templates.TemplateResponse(
        request,
        "tickets/_mismatch_warning.html",
        {"request": request, "warning": warning},
    )


@router.get("/tickets/product-options", response_class=HTMLResponse)
def tickets_product_options(
    request: Request,
    transaction_type: str | None = Query(None),
    product_id: str | None = Query(None),
    ticket_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product_id_values = request.query_params.getlist("product_id")
    parsed_product_id = None
    for raw in product_id_values:
        candidate = _parse_int(str(raw).strip())
        if candidate is not None:
            parsed_product_id = candidate
            break
    if parsed_product_id is None and product_id is not None:
        parsed_product_id = _parse_int(str(product_id).strip())

    parsed_ticket_id = _parse_int(str(ticket_id).strip()) if ticket_id else None
    form_data = {"product_id": str(parsed_product_id or "")}
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=transaction_type,
        selected_product_id=parsed_product_id,
    )
    product_usage_warning = _sales_only_selected_product_warning(
        db, transaction_type, parsed_product_id
    )
    return templates.TemplateResponse(
        request,
        "tickets/_product_field.html",
        {
            "request": request,
            "ticket_id": parsed_ticket_id or "",
            "form": form_data,
            "options": options,
            "product_unit_meta": _load_product_unit_meta(db),
            "product_usage_warning": product_usage_warning,
            "oob_product_warning": True,
        },
    )


def _ticket_direction_options() -> list[tuple[str, str]]:
    return [
        ("INWARD", "Inward"),
        ("OUTWARD", "Outward"),
    ]


def _ticket_transaction_type_options() -> list[tuple[str, str]]:
    return [
        ("WASTEIN", "Waste In"),
        ("WASTEOUT", "Waste Out"),
        ("SALE", "Sale"),
    ]


def _load_ticket_options_with_enums(
    db: Session,
    *,
    transaction_type: str | None = None,
    selected_product_id: int | None = None,
) -> dict:
    return {
        **_load_ticket_options(
            db,
            transaction_type=transaction_type,
            selected_product_id=selected_product_id,
        ),
        "directions": _ticket_direction_options(),
        "transaction_types": _ticket_transaction_type_options(),
    }


def _load_product_unit_meta(db: Session | None) -> dict[str, dict[str, str]]:
    if db is None:
        return {}
    products = db.execute(
        select(Product)
        .options(joinedload(Product.unit), joinedload(Product.ewc_code))
        .order_by(Product.id)
    ).scalars().all()
    meta: dict[str, dict[str, str]] = {}
    for product in products:
        unit = product.unit
        ewc = product.ewc_code
        meta[str(product.id)] = {
            "unit_type": (unit.unit_type if unit and unit.unit_type else ""),
            "unit_name": (unit.name if unit and unit.name else ""),
            "ewc_code_6": (ewc.code_6 if ewc and ewc.code_6 else ""),
            "ewc_code_display": (ewc.code_display if ewc and ewc.code_display else ""),
            "ewc_hazardous": "1" if ewc and ewc.hazardous else "0",
        }
    return meta


@router.get("/tickets/vehicle-suggest", response_class=HTMLResponse)
def tickets_vehicle_suggest(
    request: Request,
    reg: str | None = Query(None),
    ticket_id: int | None = Query(None),
    walk_in: str | None = Query(None),
    direction: str | None = Query(None),
    customer_id: str | None = Query(None),
    haulier_id: str | None = Query(None),
    driver_id: str | None = Query(None),
    gross_kg: str | None = Query(None),
    tare_kg: str | None = Query(None),
    readout_kg: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not reg or not ticket_id:
        return HTMLResponse("", status_code=204)
    if walk_in and str(walk_in).lower() in ("1", "true", "on", "yes"):
        return HTMLResponse("", status_code=204)

    ticket = db.get(Ticket, ticket_id)
    if not ticket or _is_ticket_locked(ticket):
        return HTMLResponse("", status_code=204)
    if ticket.walk_in:
        return HTMLResponse("", status_code=204)

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return HTMLResponse("", status_code=204)

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    default_driver = db.get(Driver, vehicle.driver_id) if vehicle.driver_id else None
    selected_customer_id = _parse_int(str(customer_id or "").strip())
    selected_haulier_id = _parse_int(str(haulier_id or "").strip())
    selected_driver_id = _parse_int(str(driver_id or "").strip())

    suggested_tare_kg = None
    if vehicle.default_tare_kg is not None:
        tare_value = Decimal(str(vehicle.default_tare_kg))
        if tare_value > 0:
            suggested_tare_kg = tare_value

    errors: list[str] = []
    warnings: list[str] = []
    if default_customer and default_customer.on_stop:
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    if default_haulier and _is_haulier_on_stop(default_haulier):
        warnings.append(
            "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    if default_haulier and not default_haulier.is_active:
        errors.append("Suggested haulier is inactive.")
    if default_driver and not default_driver.is_active:
        errors.append("Suggested driver is inactive.")

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        default_driver,
        suggested_tare_kg=suggested_tare_kg,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        current_customer_id=selected_customer_id,
        current_haulier_id=selected_haulier_id,
        current_driver_id=selected_driver_id,
        oob_customer_options=None,
        oob_haulier_options=None,
        oob_driver_options=None,
        oob_weights_form=None,
        oob_tare_auto_hint=None,
        warnings=warnings,
        errors=errors,
    )


def _generate_ticket_no(db: Session, now: datetime | None = None) -> str:
    current_time = now or utcnow()
    year = current_time.year
    db.execute(
        text(
            "INSERT OR IGNORE INTO ticket_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": current_time},
    )
    db.execute(
        text(
            "UPDATE ticket_sequences "
            "SET last_number = last_number + 1, updated_at = :updated_at "
            "WHERE year = :year"
        ),
        {"year": year, "updated_at": current_time},
    )
    next_number = db.execute(
        text("SELECT last_number FROM ticket_sequences WHERE year = :year"),
        {"year": year},
    ).scalar_one()

    return f"{str(year)[2:]}-{next_number:05d}"


def _load_ticket_options(
    db: Session | None,
    *,
    transaction_type: str | None = None,
    selected_product_id: int | None = None,
) -> dict[str, list[tuple[int, str]]]:
    if db is None:
        return {key: [] for key in _option_keys()}

    def as_options(rows, label_fn):
        return [(str(row.id), label_fn(row)) for row in rows]

    resolved_transaction_type = _enum_value_or_text(transaction_type).upper()
    show_sales_only = not _is_waste_transaction(resolved_transaction_type)
    products_stmt = (
        select(Product)
        .options(joinedload(Product.unit))
        .join(Unit, Product.unit_id == Unit.id)
        .where(Unit.is_active.is_(True))
        .order_by(Product.description)
    )
    if not show_sales_only:
        products_stmt = products_stmt.where(Product.sales_only.is_(False))
    product_rows = db.execute(products_stmt).scalars().all()
    product_options = as_options(product_rows, lambda row: row.description)
    selected_product_id_str = str(selected_product_id or "")
    has_selected_product = any(
        option_id == selected_product_id_str for option_id, _ in product_options
    )
    if (
        selected_product_id
        and not has_selected_product
        and not show_sales_only
    ):
        selected_product = (
            db.execute(
                select(Product)
                .options(joinedload(Product.unit))
                .where(Product.id == selected_product_id)
            )
            .scalars()
            .first()
        )
        if (
            selected_product
            and selected_product.sales_only
            and _product_has_active_unit(selected_product)
        ):
            product_options = [
                (
                    str(selected_product.id),
                    f"{selected_product.description} (sales only)",
                )
            ] + product_options

    def _active_void_reasons_seeded() -> list[VoidReason]:
        reasons = (
            db.execute(
                select(VoidReason)
                .where(VoidReason.is_active.is_(True))
                .order_by(VoidReason.code)
            )
            .scalars()
            .all()
        )
        if reasons:
            return reasons
        # Keep voiding operational in clean databases and long-running dev sessions.
        seed_void_reasons(db)
        return (
            db.execute(
                select(VoidReason)
                .where(VoidReason.is_active.is_(True))
                .order_by(VoidReason.code)
            )
            .scalars()
            .all()
        )

    return {
        "customers": as_options(
            db.execute(select(Customer).order_by(Customer.name)).scalars().all(),
            lambda row: row.name,
        ),
        "vehicles": as_options(
            db.execute(select(Vehicle).order_by(Vehicle.registration)).scalars().all(),
            lambda row: row.registration,
        ),
        "products": product_options,
        "hauliers": as_options(
            db.execute(select(Haulier).order_by(Haulier.name)).scalars().all(),
            lambda row: row.name,
        ),
        "drivers": as_options(
            db.execute(select(Driver).order_by(Driver.name)).scalars().all(),
            lambda row: row.name,
        ),
        "containers": as_options(
            db.execute(select(Container).order_by(Container.name)).scalars().all(),
            lambda row: row.name,
        ),
        "destinations": as_options(
            db.execute(select(Destination).order_by(Destination.name)).scalars().all(),
            lambda row: row.name,
        ),
        "ewc_codes": [
            (row.code_display, row.description)
            for row in db.execute(
                select(EwcCode)
                .where(EwcCode.active.is_(True))
                .order_by(EwcCode.code_6)
            )
            .scalars()
            .all()
        ],
        "yards": as_options(
            db.execute(select(Yard).order_by(Yard.code)).scalars().all(),
            lambda row: row.code,
        ),
        "areas": as_options(
            db.execute(select(Area).order_by(Area.code)).scalars().all(),
            lambda row: row.code,
        ),
        "void_reasons": as_options(
            _active_void_reasons_seeded(),
            lambda row: row.description or row.code,
        ),
    }


def _active_lookup_options(ticket: Ticket, db: Session) -> dict[str, list[tuple[str, str]]]:
    def active_options(model, current_id):
        rows = (
            db.execute(
                select(model)
                .where(model.is_active.is_(True))
                .order_by(model.name)
            )
            .scalars()
            .all()
        )
        options = [(str(row.id), row.name) for row in rows]
        if current_id is None:
            return options

        if any(str(row_id) == str(current_id) for row_id, _ in options):
            return options

        current = db.get(model, current_id)
        if current is None:
            return options

        label = f"{current.name} (inactive)"
        return [(str(current.id), label)] + options

    return {
        "hauliers": active_options(Haulier, ticket.haulier_id),
        "drivers": active_options(Driver, ticket.driver_id),
        "containers": active_options(Container, ticket.container_id),
        "destinations": active_options(Destination, ticket.destination_id),
    }


def _oob_lookup_options(
    ticket: Ticket, db: Session
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    options = _load_ticket_options(db)
    active = _active_lookup_options(ticket, db)
    return options["customers"], active["hauliers"]


def _option_keys() -> list[str]:
    return [
        "customers",
        "vehicles",
        "products",
        "hauliers",
        "drivers",
        "containers",
        "destinations",
        "ewc_codes",
        "yards",
        "areas",
        "void_reasons",
    ]


def _enum_value_or_text(value: object) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _sales_only_selected_product_warning(
    db: Session,
    transaction_type: object,
    selected_product_id: int | None,
) -> str | None:
    if not selected_product_id:
        return None
    if not _is_waste_transaction(_enum_value_or_text(transaction_type).upper()):
        return None
    selected_product = db.get(Product, selected_product_id)
    if not selected_product:
        return None
    if selected_product.sales_only:
        return SALES_ONLY_WASTE_WARNING
    return None


def _normalize_registration(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", "", str(value)).upper()
    return cleaned


def _normalize_reg_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", "", str(value)).upper()
    return cleaned


def _find_vehicle_by_reg(db: Session, reg: str) -> Vehicle | None:
    normalized = _normalize_registration(reg)
    if not normalized:
        return None
    return (
        db.execute(
            select(Vehicle).where(
                func.replace(func.upper(Vehicle.registration), " ", "") == normalized
            )
        )
        .scalars()
        .first()
    )


def _ticket_vehicle_reg(db: Session, ticket: Ticket) -> str:
    if ticket.vehicle_id:
        vehicle = db.get(Vehicle, ticket.vehicle_id)
        if vehicle and vehicle.registration:
            return vehicle.registration
    return ticket.vehicle_reg_text or ""


async def _resolve_vehicle_for_defaults(
    request: Request, ticket: Ticket, db: Session
) -> Vehicle | None:
    if ticket.vehicle_id:
        return db.get(Vehicle, ticket.vehicle_id)
    if ticket.walk_in:
        return None

    form = await request.form()
    if _form_value(form, "walk_in") == "on":
        return None
    reg = str(form.get("reg", "")).strip()
    if not reg:
        return None
    vehicle = _find_vehicle_by_reg(db, reg)
    if vehicle:
        ticket.vehicle_id = vehicle.id
    return vehicle


def _status_value(value) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _is_ticket_locked(ticket: Ticket) -> bool:
    return _status_value(ticket.status) in LOCKED_STATUSES


def _can_void_ticket(ticket: Ticket) -> bool:
    return (
        _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
        and ticket.invoice_id is None
    )


def _ticket_locked_message(ticket: Ticket) -> str:
    if _status_value(ticket.status) == TicketStatusEnum.VOID.value:
        return "This ticket is void and cannot be edited."
    return "This ticket is complete and cannot be edited."


def _ticket_void_blocked_message(ticket: Ticket) -> str:
    if ticket.invoice_id is not None:
        return "Cannot void a ticket that has already been invoiced."
    if _status_value(ticket.status) == TicketStatusEnum.VOID.value:
        return "This ticket is void and cannot be edited."
    if _status_value(ticket.status) != TicketStatusEnum.COMPLETE.value:
        return "Only complete tickets can be voided."
    return "Cannot void this ticket."


def _ensure_ticket_void_reasons(db: Session) -> None:
    # Keep ticket voiding operational in clean databases.
    seed_void_reasons(db)


def _latest_ticket_void_with_reason(
    db: Session, ticket_id: int
) -> tuple[TicketVoid | None, VoidReason | None]:
    row = db.execute(
        select(TicketVoid, VoidReason)
        .outerjoin(VoidReason, TicketVoid.reason_id == VoidReason.id)
        .where(TicketVoid.ticket_id == ticket_id)
        .order_by(TicketVoid.voided_at.desc(), TicketVoid.id.desc())
        .limit(1)
    ).first()
    if not row:
        return None, None
    return row[0], row[1]


def _is_haulier_on_stop(haulier: Haulier | None) -> bool:
    return bool(getattr(haulier, "on_stop", False)) if haulier else False


def _is_other_void_reason(reason: VoidReason | None) -> bool:
    if not reason:
        return False
    code = (reason.code or "").strip().lower()
    description = (reason.description or "").strip().lower()
    return code == "other" or description == "other"


def _on_stop_blockers(
    db: Session, customer_id: int | None, haulier_id: int | None
) -> list[str]:
    blockers: list[str] = []
    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer and customer.on_stop:
            blockers.append("Customer")
    if haulier_id:
        haulier = db.get(Haulier, haulier_id)
        if _is_haulier_on_stop(haulier):
            blockers.append("Haulier")
    return blockers


def _on_stop_completion_error(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
    else:
        subject = "Customer and Haulier"
    return (
        f"Cannot complete ticket: {subject} is ON STOP. "
        "You may record the ticket, but it cannot be completed/invoiced until stop is removed."
    )


def _on_stop_banner_message(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
    else:
        subject = "Customer and Haulier"
    return (
        f"{subject} is ON STOP - allowed to record ticket; cannot complete/invoice."
    )


def _expected_weigh_in_field(direction) -> str:
    direction_value = _status_value(direction)
    return "tare_kg" if direction_value == DirectionEnum.OUTWARD.value else "gross_kg"


def _validate_weighing_order(
    direction, gross_kg: float | None, tare_kg: float | None
) -> list[str]:
    if gross_kg is None and tare_kg is None:
        return []

    expected_weigh_in = _expected_weigh_in_field(direction)
    if expected_weigh_in == "gross_kg" and tare_kg is not None and gross_kg is None:
        return ["Weigh-in (gross) is required before tare."]
    if expected_weigh_in == "tare_kg" and gross_kg is not None and tare_kg is None:
        return ["Weigh-in (tare) is required before gross."]
    return []


def _is_open_ticket(ticket: Ticket) -> bool:
    return _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def _freeze_lookup_fields(ticket: Ticket, payload: dict) -> None:
    for key in ("haulier_id", "driver_id", "container_id", "destination_id"):
        current_value = getattr(ticket, key)
        payload[key] = current_value
        form_data = payload.get("form")
        if isinstance(form_data, dict):
            form_data[key] = str(current_value or "")


def _validate_lookup_fields(
    ticket: Ticket, payload: dict, db: Session
) -> list[str]:
    if not _is_open_ticket(ticket):
        _freeze_lookup_fields(ticket, payload)
        return []

    errors: list[str] = []
    form_data = payload.get("form") if isinstance(payload.get("form"), dict) else None
    checks = (
        ("haulier_id", Haulier, "Haulier"),
        ("driver_id", Driver, "Driver"),
        ("container_id", Container, "Container"),
        ("destination_id", Destination, "Destination"),
    )
    for field, model, label in checks:
        raw_value = payload.get(field)
        if raw_value in (None, ""):
            payload[field] = None
            if form_data is not None:
                form_data[field] = ""
            continue
        if isinstance(raw_value, int):
            value = raw_value
        else:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                errors.append(f"{label} not found.")
                payload[field] = None
                if form_data is not None:
                    form_data[field] = ""
                continue
        payload[field] = value
        if form_data is not None:
            form_data[field] = str(value)
        record = db.get(model, value)
        if not record:
            errors.append(f"{label} not found.")
            continue
        if not record.is_active and value != getattr(ticket, field):
            errors.append(f"{label} is inactive.")

    return errors


@router.get("/tickets/{ticket_id:int}", response_class=HTMLResponse)
def tickets_edit(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(request, 
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    _ensure_ticket_void_reasons(db)
    is_admin = False
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    ticket_void, ticket_void_reason = _latest_ticket_void_with_reason(db, ticket.id)
    stop_blockers = _on_stop_blockers(db, ticket.customer_id, ticket.haulier_id)
    form = _ticket_to_form(ticket)
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=form.get("transaction_type"),
        selected_product_id=ticket.product_id,
    )
    product_warning = _sales_only_selected_product_warning(
        db, form.get("transaction_type"), ticket.product_id
    )
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": [],
            "warnings": [],
            "product_usage_warning": product_warning,
            "saved": request.query_params.get("saved") == "1",
            "completed": request.query_params.get("completed") == "1",
            "voided": request.query_params.get("voided") == "1",
            "ticket": ticket,
            "ticket_void": ticket_void,
            "ticket_void_reason": ticket_void_reason,
            "invoice": invoice,
            "is_admin": is_admin,
            "is_open": _is_open_ticket(ticket),
            "is_locked": _is_ticket_locked(ticket),
            "can_void": _can_void_ticket(ticket),
            "locked_message": _ticket_locked_message(ticket)
            if _is_ticket_locked(ticket)
            else "",
            "stop_blocked": bool(stop_blockers),
            "stop_banner_message": _on_stop_banner_message(stop_blockers),
            "weight_warning": _net_negative(ticket),
            "direction_warning": _direction_transaction_warning(
                ticket.direction, ticket.transaction_type
            ),
            "form": form,
            "vehicle_reg": _ticket_vehicle_reg(db, ticket),
            "options": options,
            "product_unit_meta": _load_product_unit_meta(db),
            "enums": _ticket_enums(),
            **_active_lookup_options(ticket, db),
        },
    )


@router.post("/tickets/{ticket_id:int}", response_class=HTMLResponse)
async def tickets_update(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(request, 
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    form = await request.form()
    action = str(form.get("action", "save"))
    if action == "void":
        _ensure_ticket_void_reasons(db)

    if _is_ticket_locked(ticket) and not (action == "void" and _can_void_ticket(ticket)):
        if action == "void":
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=[_ticket_void_blocked_message(ticket)],
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    payload = _parse_ticket_form(
        form, current_status=ticket.status.value if ticket.status else None
    )
    payload["form"]["direction"] = payload["direction"] or ""
    payload["form"]["transaction_type"] = payload["transaction_type"] or ""
    if payload["vehicle_id"] is None and not payload["walk_in"]:
        inferred_vehicle = (
            _find_vehicle_by_reg(db, payload["vehicle_reg_text"])
            if payload.get("vehicle_reg_text")
            else None
        )
        if inferred_vehicle:
            payload["vehicle_id"] = inferred_vehicle.id
            payload["form"]["vehicle_id"] = str(inferred_vehicle.id)
        else:
            payload["form"]["vehicle_id"] = ""
    elif payload["walk_in"]:
        payload["vehicle_id"] = None
        payload["form"]["vehicle_id"] = ""

    if action == "complete":
        ticket.direction = payload["direction"]
        ticket.transaction_type = payload["transaction_type"]
        weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        direction_warning = _direction_transaction_warning(
            payload["direction"], payload["transaction_type"]
        )
        lookup_errors = _validate_lookup_fields(ticket, payload, db)
        payload["errors"].extend(lookup_errors)
        _apply_ticket_defaults(db, payload)
        product = _validate_product_ewc(payload, db)
        ewc_snapshot = _resolve_ticket_ewc_snapshot(payload, db, product)
        _coerce_mode_fields(payload, product)
        weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        _apply_destination_default(ticket, payload, product)
        haulier = _validate_carrier_licence(payload, db)
        _resolve_waste_producer_snapshot(
            payload,
            db,
            require_customer_when_same=True,
            require_manual_name=True,
        )
        is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
        if is_waste_tx and haulier and not haulier.carrier_licence_number:
            payload["errors"].append(
                "Selected haulier has no Waste Carrier Licence number. "
                "Add it in Lookups → Hauliers, then complete the ticket."
            )

        effective_reg_text = payload["vehicle_reg_text"] or ticket.vehicle_reg_text or ""
        completion_allowed = (
            payload["walk_in"]
            or payload["vehicle_id"] is not None
            or bool(effective_reg_text)
        )
        if is_waste_tx and not completion_allowed:
            payload["errors"].append(
                "To complete: enter a registration, select a vehicle, or tick 'No vehicle / Walk-in'."
            )

        _validate_required_on_complete(payload)
        stop_blockers = _on_stop_blockers(
            db, payload.get("customer_id"), payload.get("haulier_id")
        )
        if stop_blockers:
            payload["errors"].append(_on_stop_completion_error(stop_blockers))
        product_unit_type = _product_unit_type(product)
        has_qty = payload.get("qty") is not None and payload.get("qty") > 0
        has_gross = payload.get("gross_kg") is not None
        has_tare = payload.get("tare_kg") is not None and payload.get("tare_kg") > 0
        if is_waste_tx and product_unit_type != "COUNT" and has_gross and not has_tare:
            vehicle = (
                db.get(Vehicle, payload["vehicle_id"])
                if payload.get("vehicle_id")
                else None
            )
            if vehicle and vehicle.default_tare_kg is not None:
                default_tare_kg = float(vehicle.default_tare_kg)
                if default_tare_kg > 0:
                    payload["tare_kg"] = default_tare_kg
                    payload["form"]["tare_kg"] = f"{default_tare_kg:.0f}"
                    payload["net_kg"] = payload["gross_kg"] - payload["tare_kg"]
                    payload["form"]["net_kg"] = f"{payload['net_kg']:.0f}"
                    has_tare = True
        has_weights = has_gross and has_tare
        if product_unit_type == "COUNT":
            count_mode_error = "COUNT product: enter Qty only (weights must be blank)."
            if not has_qty:
                if count_mode_error not in payload["errors"]:
                    payload["errors"].append(count_mode_error)
        elif product_unit_type == "WEIGHT":
            weight_mode_error = "WEIGHT product: enter weights only (Qty must be blank)."
            if has_qty or not has_weights:
                if weight_mode_error not in payload["errors"]:
                    payload["errors"].append(weight_mode_error)
        else:
            if is_waste_tx and not has_weights:
                payload["errors"].append(
                    "Weigh-in and weigh-out are required to complete a waste ticket."
                )
            if (
                not is_waste_tx
                and payload.get("transaction_type") == TransactionTypeEnum.SALE.value
            ):
                allow_sale_weights = bool(
                    has_weights and product and product.unit and product.unit.unit_type == "WEIGHT"
                )
                if not (has_qty or allow_sale_weights):
                    payload["errors"].append(
                        "Enter a quantity or weigh-in and weigh-out to complete a sale."
                    )
        if (
            product_unit_type != "COUNT"
            and
            has_weights
            and _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        ):
            payload["errors"].append(
                "Net weight cannot be negative. Use Swap Weights."
            )

        _validate_waste_ewc_on_complete(payload, ewc_snapshot)

        if payload["errors"]:
            ticket.vehicle_reg_text = payload["vehicle_reg_text"]
            ticket.walk_in = payload["walk_in"]
            ticket.updated_at = utcnow()
            db.commit()
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=payload["errors"],
                warnings=payload.get("warnings"),
                form=payload["form"],
                vehicle_reg=payload["vehicle_reg_text"],
                weight_warning=weight_warning,
                direction_warning=direction_warning,
                status_code=400,
            )

        _apply_ticket_updates(ticket, payload)
        pricing_info = _apply_ticket_pricing(ticket, payload, product)
        _apply_ticket_ewc_snapshot(ticket, ewc_snapshot)
        _apply_carrier_licence_snapshot(ticket, haulier)
        _apply_waste_producer_snapshot(ticket, payload)
        is_hazardous = bool(ticket.ewc_hazardous)
        if is_waste_tx and is_hazardous:
            if not ticket.customer_id:
                payload["errors"].append(
                    "Customer is required for hazardous waste tickets."
                )
            if not ticket.waste_producer_name:
                payload["errors"].append(
                    "Waste producer details are required for hazardous waste tickets."
                )
        if payload["errors"]:
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=payload["errors"],
                warnings=payload.get("warnings"),
                form=payload["form"],
                vehicle_reg=payload["vehicle_reg_text"],
                weight_warning=weight_warning,
                direction_warning=direction_warning,
                status_code=400,
            )
        unit = getattr(product, "unit", None) if product else None
        ticket.pricing_unit_name = unit.name if unit else None
        ticket.pricing_unit_type = unit.unit_type if unit else None
        ticket.pricing_unit_price = ticket.unit_price
        ticket.pricing_qty_snapshot = ticket.qty
        ticket.pricing_net_kg_snapshot = (
            pricing_info.get("net_kg") if pricing_info else None
        ) or ticket.net_kg
        ticket.pricing_billable_qty_snapshot = (
            pricing_info.get("billable_qty") if pricing_info else None
        )
        ticket.status = TicketStatusEnum.COMPLETE.value
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}?completed=1", status_code=303)

    if action == "void":
        if not _can_void_ticket(ticket):
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=[_ticket_void_blocked_message(ticket)],
                status_code=400,
            )
        reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
        note = str(form.get("void_note", "")).strip()
        errors = []
        validate_no_html(note, "Void note", errors)
        if not reason_id:
            errors.append("Void reason is required.")
        reason = db.get(VoidReason, reason_id) if reason_id else None
        if reason_id and (not reason or not reason.is_active):
            errors.append("Void reason is invalid.")
        if reason and _is_other_void_reason(reason) and not note:
            errors.append("Void note is required when reason is Other.")
        if errors:
            return _render_ticket_edit(
                request,
                ticket,
                db,
                errors=errors,
                status_code=400,
            )

        ticket.status = TicketStatusEnum.VOID.value
        db.add(
            TicketVoid(
                ticket_id=ticket.id,
                reason_id=reason_id,
                note=note or "No note provided.",
                voided_at=utcnow(),
                voided_by="admin",
            )
        )
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}?voided=1", status_code=303)

    ticket.direction = payload["direction"]
    ticket.transaction_type = payload["transaction_type"]
    direction_warning = _direction_transaction_warning(
        payload["direction"], payload["transaction_type"]
    )
    _apply_ticket_defaults(db, payload)
    weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
    lookup_errors = _validate_lookup_fields(ticket, payload, db)
    payload["errors"].extend(lookup_errors)
    product = _validate_product_ewc(payload, db)
    ewc_snapshot = _resolve_ticket_ewc_snapshot(payload, db, product)
    _coerce_mode_fields(payload, product)
    weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
    weighing_order_errors = _validate_weighing_order(
        payload["direction"], payload["gross_kg"], payload["tare_kg"]
    )
    if (
        weighing_order_errors == ["Weigh-in (gross) is required before tare."]
        and payload.get("gross_kg") is None
        and payload.get("tare_kg") is not None
        and payload.get("vehicle_id")
    ):
        vehicle = db.get(Vehicle, payload["vehicle_id"])
        if vehicle and vehicle.default_tare_kg is not None:
            default_tare_kg = float(vehicle.default_tare_kg)
            if default_tare_kg > 0 and abs(payload["tare_kg"] - default_tare_kg) < 0.0001:
                weighing_order_errors = []
    payload["errors"].extend(weighing_order_errors)
    _apply_destination_default(ticket, payload, product)
    haulier = _validate_carrier_licence(payload, db)
    _resolve_waste_producer_snapshot(payload, db)
    is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
    if payload["errors"]:
        ticket.vehicle_reg_text = payload["vehicle_reg_text"]
        ticket.walk_in = payload["walk_in"]
        ticket.updated_at = utcnow()
        db.commit()
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=payload["errors"],
            warnings=payload.get("warnings"),
            form=payload["form"],
            vehicle_reg=payload["vehicle_reg_text"],
            weight_warning=weight_warning,
            direction_warning=direction_warning,
            status_code=400,
        )

    _apply_ticket_updates(ticket, payload)
    _apply_ticket_pricing(ticket, payload, product)
    _apply_ticket_ewc_snapshot(ticket, ewc_snapshot)
    _apply_carrier_licence_snapshot(ticket, haulier)
    _apply_waste_producer_snapshot(ticket, payload)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}?saved=1", status_code=303)


@router.post("/tickets/vehicle-suggestion/apply", response_class=HTMLResponse)
async def tickets_apply_vehicle_suggestion(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    reg = _form_value(form, "reg")
    if not reg:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Registration is required."],
            status_code=400,
        )

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle not found."],
            status_code=404,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    default_driver = db.get(Driver, vehicle.driver_id) if vehicle.driver_id else None
    suggested_tare_kg = None
    if vehicle.default_tare_kg is not None:
        candidate_tare = Decimal(str(vehicle.default_tare_kg))
        if candidate_tare > 0:
            suggested_tare_kg = candidate_tare

    errors: list[str] = []
    warnings: list[str] = []
    applied = False
    tare_applied = False

    if ticket.vehicle_id != vehicle.id:
        ticket.vehicle_id = vehicle.id
        applied = True
    if (ticket.vehicle_reg_text or "") != vehicle.registration:
        ticket.vehicle_reg_text = vehicle.registration
        applied = True

    if default_customer:
        if ticket.customer_id != default_customer.id:
            ticket.customer_id = default_customer.id
            applied = True
        if default_customer.on_stop:
            warnings.append(
                "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
            )

    if default_haulier:
        if not default_haulier.is_active:
            errors.append("Suggested haulier is inactive.")
        else:
            if ticket.haulier_id != default_haulier.id:
                ticket.haulier_id = default_haulier.id
                applied = True
            if _is_haulier_on_stop(default_haulier):
                warnings.append(
                    "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
                )

    if default_driver:
        if not default_driver.is_active:
            errors.append("Suggested driver is inactive.")
        elif ticket.driver_id != default_driver.id:
            ticket.driver_id = default_driver.id
            applied = True

    if suggested_tare_kg is not None:
        current_tare = Decimal(str(ticket.tare_kg)) if ticket.tare_kg is not None else None
        if current_tare is None or current_tare != suggested_tare_kg:
            ticket.tare_kg = suggested_tare_kg
            applied = True
            tare_applied = True
        gross_value = Decimal(str(ticket.gross_kg)) if ticket.gross_kg is not None else None
        ticket.net_kg = (
            gross_value - ticket.tare_kg
            if gross_value is not None and ticket.tare_kg is not None
            else None
        )

    if not applied:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            default_driver,
            suggested_tare_kg=suggested_tare_kg,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            warnings=warnings,
            errors=errors or ["No defaults available to apply for this vehicle."],
            status_code=400,
        )

    ticket.updated_at = utcnow()
    db.commit()

    options = _load_ticket_options(db)
    active_options = _active_lookup_options(ticket, db)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        default_driver,
        suggested_tare_kg=suggested_tare_kg,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        current_customer_id=ticket.customer_id,
        current_haulier_id=ticket.haulier_id,
        current_driver_id=ticket.driver_id,
        oob_customer_options=options["customers"],
        oob_haulier_options=active_options["hauliers"],
        oob_driver_options=active_options["drivers"],
        oob_weights_form=_ticket_to_form(ticket) if tare_applied else None,
        oob_tare_auto_hint="Tare applied from vehicle default." if tare_applied else None,
        oob_status_warnings=warnings,
        show_panel=False,
        warnings=warnings,
        errors=errors,
    )


@router.post("/tickets/vehicle-suggestion/dismiss", response_class=HTMLResponse)
def tickets_dismiss_vehicle_suggestion() -> HTMLResponse:
    return HTMLResponse("", status_code=200)


@router.post("/tickets/{ticket_id}/apply-default-customer", response_class=HTMLResponse)
async def tickets_apply_default_customer(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )
    warnings: list[str] = []

    if ticket.customer_id:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Customer already set."],
            status_code=400,
        )
    if not default_customer:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default customer not configured for this vehicle."],
            status_code=400,
        )
    if default_customer.on_stop:
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )

    ticket.customer_id = default_customer.id
    ticket.updated_at = utcnow()
    db.commit()
    oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
    show_panel = not (ticket.haulier_id is not None or default_haulier is None)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        oob_customer_options=oob_customer_options,
        oob_haulier_options=oob_haulier_options,
        oob_status_warnings=warnings,
        show_panel=show_panel,
        warnings=warnings,
    )


@router.post("/tickets/{ticket_id}/apply-default-haulier", response_class=HTMLResponse)
async def tickets_apply_default_haulier(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    if ticket.haulier_id:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Haulier already set."],
            status_code=400,
        )
    if not default_haulier:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default haulier not configured for this vehicle."],
            status_code=400,
        )
    if not default_haulier.is_active:
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default haulier is inactive."],
            status_code=400,
        )

    warnings: list[str] = []
    ticket.haulier_id = default_haulier.id
    if _is_haulier_on_stop(default_haulier):
        warnings.append(
            "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    ticket.updated_at = utcnow()
    db.commit()
    oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
    show_panel = not (ticket.customer_id is not None or default_customer is None)
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        oob_customer_options=oob_customer_options,
        oob_haulier_options=oob_haulier_options,
        oob_status_warnings=warnings,
        show_panel=show_panel,
        warnings=warnings,
    )


@router.post("/tickets/{ticket_id}/apply-defaults", response_class=HTMLResponse)
async def tickets_apply_defaults(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    vehicle = await _resolve_vehicle_for_defaults(request, ticket, db)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle must be selected to apply defaults."],
            status_code=400,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    errors: list[str] = []
    warnings: list[str] = []
    updates = 0

    if ticket.customer_id:
        errors.append("Customer already set.")
    elif not default_customer:
        errors.append("Default customer not configured for this vehicle.")
    elif default_customer.on_stop:
        ticket.customer_id = default_customer.id
        updates += 1
        warnings.append(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
    else:
        ticket.customer_id = default_customer.id
        updates += 1

    if ticket.haulier_id:
        errors.append("Haulier already set.")
    elif not default_haulier:
        errors.append("Default haulier not configured for this vehicle.")
    elif not default_haulier.is_active:
        errors.append("Default haulier is inactive.")
    else:
        ticket.haulier_id = default_haulier.id
        updates += 1
        if _is_haulier_on_stop(default_haulier):
            warnings.append(
                "Haulier is ON STOP - allowed to record ticket; cannot complete/invoice."
            )

    if updates:
        ticket.updated_at = utcnow()
        db.commit()
        oob_customer_options, oob_haulier_options = _oob_lookup_options(ticket, db)
        show_panel = bool(errors)
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            oob_customer_options=oob_customer_options,
            oob_haulier_options=oob_haulier_options,
            oob_status_warnings=warnings,
            show_panel=show_panel,
            warnings=warnings,
            errors=errors,
        )

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
        warnings=warnings,
        errors=errors or ["No defaults applied."],
        status_code=400,
    )


@router.post("/tickets/{ticket_id}/set-vehicle-from-reg", response_class=HTMLResponse)
async def tickets_set_vehicle_from_reg(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    form = await request.form()
    reg = str(form.get("reg", "")).strip()
    if not reg:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Registration is required."],
            status_code=400,
        )

    vehicle = _find_vehicle_by_reg(db, reg)
    if not vehicle:
        return _render_vehicle_suggestions(
            request,
            ticket,
            None,
            None,
            None,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Vehicle not found."],
            status_code=404,
        )

    default_customer = (
        db.get(Customer, vehicle.default_customer_id)
        if vehicle.default_customer_id
        else None
    )
    default_haulier = (
        db.get(Haulier, vehicle.default_haulier_id)
        if vehicle.default_haulier_id
        else None
    )

    if ticket.vehicle_id:
        current_vehicle = db.get(Vehicle, ticket.vehicle_id)
        if current_vehicle and current_vehicle.id == vehicle.id:
            return _render_vehicle_suggestions(
                request,
                ticket,
                vehicle,
                default_customer,
                default_haulier,
                ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
                status_code=200,
            )
        current_reg = current_vehicle.registration if current_vehicle else "(unknown)"
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=current_reg,
            errors=[
                (
                    "Ticket is linked to "
                    f"{current_reg}. This registration matches {vehicle.registration}."
                )
            ],
            status_code=400,
        )

    ticket.vehicle_id = vehicle.id
    ticket.updated_at = utcnow()
    db.commit()
    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
    )


@router.post("/tickets/{ticket_id}/weights/gross", response_class=HTMLResponse)
async def tickets_capture_gross(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    if ticket.gross_kg is not None:
        return _render_weights_partial(
            request, ticket, errors=["Gross weight already recorded."], status_code=400
        )
    if _expected_weigh_in_field(ticket.direction) == "tare_kg" and ticket.tare_kg is None:
        return _render_weights_partial(
            request,
            ticket,
            errors=["Weigh-in (tare) is required before gross."],
            status_code=400,
        )

    form = await request.form()
    errors: list[str] = []
    gross_value = _parse_weight_value(
        _form_value(form, "weight_value"), "Gross weight", errors
    )
    if gross_value is None:
        if not errors:
            errors.append("Gross weight is required.")
        return _render_weights_partial(
            request, ticket, errors=errors, status_code=400
        )

    ticket.gross_kg = gross_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = utcnow()
    db.commit()
    return _render_weights_partial(request, ticket, errors=[])




@router.post("/tickets/{ticket_id}/weights/tare", response_class=HTMLResponse)
async def tickets_capture_tare(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    if ticket.tare_kg is not None:
        return _render_weights_partial(
            request, ticket, errors=["Tare weight already recorded."], status_code=400
        )
    if _expected_weigh_in_field(ticket.direction) == "gross_kg" and ticket.gross_kg is None:
        return _render_weights_partial(
            request,
            ticket,
            errors=["Weigh-in (gross) is required before tare."],
            status_code=400,
        )

    form = await request.form()
    errors: list[str] = []
    tare_value = _parse_weight_value(
        _form_value(form, "weight_value"), "Tare weight", errors
    )
    if tare_value is None:
        if not errors:
            errors.append("Tare weight is required.")
        return _render_weights_partial(
            request, ticket, errors=errors, status_code=400
        )

    ticket.tare_kg = tare_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = utcnow()
    db.commit()
    return _render_weights_partial(request, ticket, errors=[])


@router.post("/tickets/weights/read", response_class=HTMLResponse)
async def tickets_read_weight(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    return _render_weights_partial(
        request,
        ticket,
        errors=["Not implemented: live weighbridge readout integration is not wired yet."],
        status_code=400,
    )


@router.post("/tickets/weights/read-apply", response_class=HTMLResponse)
async def tickets_read_weight_apply(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )
    return _render_weights_partial(
        request,
        ticket,
        errors=["Not implemented: live weighbridge readout integration is not wired yet."],
        status_code=400,
    )


@router.post("/tickets/weights/swap-preview", response_class=HTMLResponse)
async def tickets_swap_weights_preview(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    ticket_id = _parse_int(_form_value(form, "ticket_id"))
    if not ticket_id:
        return HTMLResponse("Ticket not found.", status_code=404)

    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        return _render_weights_partial(
            request,
            ticket,
            errors=[_ticket_locked_message(ticket)],
            status_code=400,
        )

    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    readout_raw = _form_value(form, "readout_kg")
    product_id = (
        _parse_int(_first_non_empty_form_value(form, "product_id"))
        or _parse_int(_first_non_empty_form_value(form, "weights_product_id"))
        or ticket.product_id
    )
    product = db.get(Product, product_id) if product_id else None
    unit_type = product.unit.unit_type if product and product.unit else ""
    weights_locked = str(unit_type or "").upper() == "COUNT"
    errors: list[str] = []

    gross_value = _parse_weight_value(gross_raw, "Gross weight", errors)
    tare_value = _parse_weight_value(tare_raw, "Tare weight", errors)

    if weights_locked:
        net_value = (
            gross_value - tare_value
            if gross_value is not None and tare_value is not None
            else None
        )
        form_data = _weights_form_from_values(
            ticket=ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            readout_raw=readout_raw,
            net_value=net_value,
        )
        form_data["product_id"] = str(product_id or "")
        return templates.TemplateResponse(
            request,
            "tickets/_weights_preview.html",
            {
                "request": request,
                "ticket": ticket,
                "errors": ["Swap weights not available for COUNT products."],
                "is_admin": False,
                "is_open": _is_open_ticket(ticket),
                "weights_locked": True,
                "unit_type": unit_type,
                "form": form_data,
                "show_weight_errors": True,
                "weight_warning": _net_negative_values(gross_value, tare_value),
            },
            status_code=400,
        )

    if gross_value is None and not gross_raw:
        errors.append("Gross weight is required.")
    if tare_value is None and not tare_raw:
        errors.append("Tare weight is required.")

    if errors:
        form_data = _weights_form_from_values(
            ticket=ticket,
            gross_raw=gross_raw,
            tare_raw=tare_raw,
            readout_raw=readout_raw,
            net_value=None,
        )
        form_data["product_id"] = str(product_id or "")
        return templates.TemplateResponse(
            request,
            "tickets/_weights_preview.html",
            {
                "request": request,
                "ticket": ticket,
                "errors": errors,
                "is_admin": False,
                "is_open": _is_open_ticket(ticket),
                "weights_locked": False,
                "unit_type": unit_type,
                "form": form_data,
                "show_weight_errors": True,
                "weight_warning": _net_negative_values(gross_value, tare_value),
            },
            status_code=400,
        )

    swapped_gross = tare_raw
    swapped_tare = gross_raw
    net_value = tare_value - gross_value
    form_data = _weights_form_from_values(
        ticket=ticket,
        gross_raw=swapped_gross,
        tare_raw=swapped_tare,
        readout_raw=readout_raw,
        net_value=net_value,
    )
    form_data["product_id"] = str(product_id or "")

    return templates.TemplateResponse(
        request,
        "tickets/_weights_preview.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": [],
            "is_admin": False,
            "is_open": _is_open_ticket(ticket),
            "weights_locked": False,
            "unit_type": unit_type,
            "form": form_data,
            "show_weight_errors": True,
            "weight_warning": _net_negative_values(tare_value, gross_value),
        },
    )


def _apply_ticket_updates(ticket: Ticket, payload: dict) -> None:
    ticket.datetime = payload["ticket_datetime"]
    ticket.status = payload["status"]
    ticket.customer_id = payload["customer_id"]
    ticket.vehicle_id = payload["vehicle_id"]
    ticket.vehicle_reg_text = payload["vehicle_reg_text"]
    ticket.walk_in = payload["walk_in"]
    ticket.product_id = payload["product_id"]
    ticket.haulier_id = payload["haulier_id"]
    ticket.driver_id = payload["driver_id"]
    ticket.container_id = payload["container_id"]
    ticket.destination_id = payload["destination_id"]
    if payload.get("yard_id_present"):
        ticket.yard_id = payload["yard_id"]
    if payload.get("area_id_present"):
        ticket.area_id = payload["area_id"]
    ticket.waste_code_id = payload["waste_code_id"]
    ticket.waste_producer_customer_id = None
    source_value = payload.get("waste_producer_source")
    ticket.waste_producer_source = (
        WasteProducerSourceEnum(source_value) if source_value else None
    )
    ticket.gross_kg = payload["gross_kg"]
    ticket.tare_kg = payload["tare_kg"]
    ticket.net_kg = payload["net_kg"]
    ticket.qty = payload["qty"]
    ticket.unit_id = payload["unit_id"]
    ticket.unit_price = payload["unit_price"]
    ticket.total = payload["total"]
    ticket.dont_invoice = payload["dont_invoice"]
    ticket.updated_at = utcnow()


def _parse_ticket_form(form, current_status: str | None = None) -> dict:
    errors: list[str] = []

    datetime_raw = _form_value(form, "datetime")
    direction_raw = _form_value(form, "direction")
    transaction_type_raw = _form_value(form, "transaction_type")
    direction = direction_raw or None
    transaction_type = transaction_type_raw or None
    status = current_status or TicketStatusEnum.OPEN.value
    customer_id = _parse_int(_form_value(form, "customer_id"))
    vehicle_id = _parse_int(_form_value(form, "vehicle_id"))
    vehicle_reg_text = _normalize_reg_text(_form_value(form, "reg"))
    walk_in = _form_value(form, "walk_in") == "on"
    product_id_raw = _first_non_empty_form_value(form, "product_id")
    product_id = _parse_int(product_id_raw)
    same_as_customer_present = bool(
        _form_value(form, "waste_producer_same_as_customer_present")
    )
    if same_as_customer_present:
        waste_producer_same_as_customer = (
            _form_value(form, "waste_producer_same_as_customer").lower()
            in {"on", "1", "true", "yes"}
        )
    else:
        # Backward compatibility for callers that do not yet send the new UI fields.
        waste_producer_same_as_customer = True
    waste_producer_name = _form_value(form, "waste_producer_name")
    waste_producer_address_line_1 = _form_value(form, "waste_producer_address_line_1")
    waste_producer_address_line_2 = _form_value(form, "waste_producer_address_line_2")
    waste_producer_address_line_3 = _form_value(form, "waste_producer_address_line_3")
    waste_producer_postcode = _form_value(form, "waste_producer_postcode")
    ewc_code_raw = _form_value(form, "ewc_code")
    ewc_manual_override = _form_value(form, "ewc_manual_override").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ewc_auto_code_6 = _normalize_ewc_digits(_form_value(form, "ewc_auto_code_6"))
    ewc_product_default_code_6 = _normalize_ewc_digits(
        _form_value(form, "ewc_product_default_code_6")
    )
    ewc_product_default_display = _form_value(form, "ewc_product_default_display")
    yard_id_present = "yard_id" in form
    area_id_present = "area_id" in form
    yard_raw = _form_value(form, "yard_id")
    area_raw = _form_value(form, "area_id")

    validate_no_html_fields(
        {
            "Vehicle registration": vehicle_reg_text,
            "Yard": yard_raw,
            "Area": area_raw,
            "Waste producer name": waste_producer_name,
            "Waste producer address line 1": waste_producer_address_line_1,
            "Waste producer address line 2": waste_producer_address_line_2,
            "Waste producer address line 3": waste_producer_address_line_3,
            "Waste producer postcode": waste_producer_postcode,
            "EWC code": ewc_code_raw,
        },
        errors,
    )

    if not datetime_raw:
        errors.append("Date/time is required.")
    # Customer/vehicle/product can be left blank on open tickets.

    if direction and direction not in _ticket_enums()["directions"]:
        errors.append("Direction must be INWARD or OUTWARD.")
    if transaction_type and transaction_type not in _ticket_enums()["transaction_types"]:
        errors.append("Transaction type is invalid.")
    if status and status not in _ticket_enums()["statuses"]:
        errors.append("Status is invalid.")

    ticket_datetime: datetime | None = None
    if datetime_raw:
        try:
            ticket_datetime = datetime.fromisoformat(datetime_raw)
        except ValueError:
            errors.append("Date/time must be valid.")

    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_kg = _parse_weight_value(gross_raw, "Gross weight", errors)
    tare_kg = _parse_weight_value(tare_raw, "Tare weight", errors)
    qty = _parse_float(_form_value(form, "qty"))
    unit_price_raw = _form_value(form, "unit_price")
    unit_price = _parse_decimal(unit_price_raw)
    net_kg = (
        gross_kg - tare_kg if gross_kg is not None and tare_kg is not None else None
    )
    total = (
        Decimal(str(qty)) * unit_price
        if qty is not None and unit_price is not None
        else None
    )
    ewc_code_6 = _parse_ewc_code_value(ewc_code_raw, errors)
    dont_invoice = _form_value(form, "dont_invoice") == "on"

    form_data = {
        "datetime": datetime_raw,
        "direction": direction or "",
        "transaction_type": transaction_type or "",
        "status": status,
        "customer_id": _form_value(form, "customer_id"),
        "vehicle_id": _form_value(form, "vehicle_id"),
        "vehicle_reg_text": vehicle_reg_text,
        "walk_in": "on" if walk_in else "",
        "product_id": product_id_raw,
        "haulier_id": _form_value(form, "haulier_id"),
        "driver_id": _form_value(form, "driver_id"),
        "container_id": _form_value(form, "container_id"),
        "destination_id": _form_value(form, "destination_id"),
        "yard_id": _form_value(form, "yard_id"),
        "area_id": _form_value(form, "area_id"),
        "waste_code_id": _form_value(form, "waste_code_id"),
        "waste_producer_same_as_customer": "on"
        if waste_producer_same_as_customer
        else "",
        "waste_producer_name": waste_producer_name,
        "waste_producer_address_line_1": waste_producer_address_line_1,
        "waste_producer_address_line_2": waste_producer_address_line_2,
        "waste_producer_address_line_3": waste_producer_address_line_3,
        "waste_producer_postcode": waste_producer_postcode,
        "ewc_code": _format_ewc_code_display(ewc_code_6)
        if ewc_code_6
        else ewc_code_raw,
        "ewc_manual_override": "1" if ewc_manual_override else "0",
        "ewc_auto_code_6": _format_ewc_code_display(ewc_auto_code_6)
        if ewc_auto_code_6
        else "",
        "ewc_product_default_code_6": ewc_product_default_code_6,
        "ewc_product_default_display": ewc_product_default_display,
        "gross_kg": gross_raw if gross_kg is None else f"{gross_kg:.0f}",
        "tare_kg": tare_raw if tare_kg is None else f"{tare_kg:.0f}",
        "net_kg": f"{net_kg:.0f}" if net_kg is not None else "",
        "qty": _form_value(form, "qty"),
        "unit_id": _form_value(form, "unit_id"),
        "unit_price": _form_value(form, "unit_price"),
        "total": f"{total:.2f}" if total is not None else "",
        "dont_invoice": "on" if dont_invoice else "",
    }

    return {
        "errors": errors,
        "warnings": [],
        "form": form_data,
        "ticket_datetime": ticket_datetime or utcnow(),
        "direction": direction,
        "transaction_type": transaction_type,
        "direction_raw": direction_raw,
        "transaction_type_raw": transaction_type_raw,
        "status": status,
        "customer_id": customer_id,
        "vehicle_id": vehicle_id,
        "vehicle_reg_text": vehicle_reg_text,
        "walk_in": walk_in,
        "product_id": product_id,
        "haulier_id": _parse_int(_form_value(form, "haulier_id")),
        "driver_id": _parse_int(_form_value(form, "driver_id")),
        "container_id": _parse_int(_form_value(form, "container_id")),
        "destination_id": _parse_int(_form_value(form, "destination_id")),
        "yard_id": _parse_int(_form_value(form, "yard_id")),
        "area_id": _parse_int(_form_value(form, "area_id")),
        "yard_id_present": yard_id_present,
        "area_id_present": area_id_present,
        "waste_code_id": _parse_int(_form_value(form, "waste_code_id")),
        "waste_producer_same_as_customer": waste_producer_same_as_customer,
        "waste_producer_name": waste_producer_name,
        "waste_producer_address_line_1": waste_producer_address_line_1,
        "waste_producer_address_line_2": waste_producer_address_line_2,
        "waste_producer_address_line_3": waste_producer_address_line_3,
        "waste_producer_postcode": waste_producer_postcode,
        "ewc_code_raw": ewc_code_raw,
        "ewc_code_6": ewc_code_6,
        "ewc_manual_override": ewc_manual_override,
        "ewc_auto_code_6": ewc_auto_code_6,
        "ewc_product_default_code_6": ewc_product_default_code_6,
        "ewc_product_default_display": ewc_product_default_display,
        "waste_producer_source": (
            WasteProducerSourceEnum.CUSTOMER.value
            if waste_producer_same_as_customer
            else WasteProducerSourceEnum.MANUAL.value
        ),
        "gross_kg": gross_kg,
        "tare_kg": tare_kg,
        "net_kg": net_kg,
        "qty": qty,
        "unit_id": _parse_int(_form_value(form, "unit_id")),
        "unit_price": unit_price,
        "total": total,
        "dont_invoice": dont_invoice,
        "unit_price_raw": unit_price_raw,
    }


def _ticket_to_form(ticket: Ticket) -> dict:
    source_value = (
        ticket.waste_producer_source.value
        if hasattr(ticket.waste_producer_source, "value")
        else str(ticket.waste_producer_source or "")
    )
    if source_value:
        same_as_customer = source_value == WasteProducerSourceEnum.CUSTOMER.value
    elif ticket.waste_producer_customer_id is not None:
        same_as_customer = True
    elif ticket.waste_producer_name or ticket.waste_producer_address:
        same_as_customer = False
    else:
        same_as_customer = True
    if same_as_customer:
        producer_name = ""
        producer_line_1 = ""
        producer_line_2 = ""
        producer_line_3 = ""
        producer_postcode = ""
    else:
        producer_name = ticket.waste_producer_name or ""
        (
            producer_line_1,
            producer_line_2,
            producer_line_3,
            producer_postcode,
        ) = _split_waste_producer_address(ticket.waste_producer_address)
    ticket_ewc_code_6 = _normalize_ewc_digits(ticket.ewc_code_6)
    product_ewc = ticket.product.ewc_code if ticket.product and ticket.product.ewc_code else None
    product_default_ewc_code_6 = _normalize_ewc_digits(
        product_ewc.code_6 if product_ewc else ""
    )
    product_default_ewc_display = (
        product_ewc.code_display
        if product_ewc and product_ewc.code_display
        else _format_ewc_code_display(product_default_ewc_code_6)
    )
    ewc_value = ""
    if ticket_ewc_code_6:
        ewc_value = (
            ticket.ewc_code_display
            or _format_ewc_code_display(ticket_ewc_code_6)
        )
    elif product_default_ewc_code_6:
        ewc_value = product_default_ewc_display
    ewc_manual_override = bool(ticket.ewc_manual_override)
    ewc_auto_code_6 = ""
    if ticket_ewc_code_6 and not ewc_manual_override:
        ewc_auto_code_6 = ticket_ewc_code_6
    elif not ticket_ewc_code_6 and product_default_ewc_code_6:
        ewc_auto_code_6 = product_default_ewc_code_6
    return {
        "datetime": ticket.datetime.isoformat(timespec="minutes")
        if ticket.datetime
        else "",
        "direction": ticket.direction.value if ticket.direction else "",
        "transaction_type": ticket.transaction_type.value if ticket.transaction_type else "",
        "status": ticket.status.value if ticket.status else "",
        "customer_id": str(ticket.customer_id or ""),
        "vehicle_id": str(ticket.vehicle_id or ""),
        "vehicle_reg_text": ticket.vehicle_reg_text or "",
        "walk_in": "on" if ticket.walk_in else "",
        "product_id": str(ticket.product_id or ""),
        "haulier_id": str(ticket.haulier_id or ""),
        "driver_id": str(ticket.driver_id or ""),
        "container_id": str(ticket.container_id or ""),
        "destination_id": str(ticket.destination_id or ""),
        "yard_id": str(ticket.yard_id or ""),
        "area_id": str(ticket.area_id or ""),
        "waste_code_id": str(ticket.waste_code_id or ""),
        "waste_producer_same_as_customer": "on" if same_as_customer else "",
        "waste_producer_name": producer_name,
        "waste_producer_address_line_1": producer_line_1,
        "waste_producer_address_line_2": producer_line_2,
        "waste_producer_address_line_3": producer_line_3,
        "waste_producer_postcode": producer_postcode,
        "ewc_code": ewc_value,
        "ewc_manual_override": "1" if ewc_manual_override else "0",
        "ewc_auto_code_6": _format_ewc_code_display(ewc_auto_code_6)
        if ewc_auto_code_6
        else "",
        "ewc_product_default_code_6": product_default_ewc_code_6,
        "ewc_product_default_display": product_default_ewc_display or "",
        "gross_kg": f"{ticket.gross_kg:.0f}" if ticket.gross_kg is not None else "",
        "tare_kg": f"{ticket.tare_kg:.0f}" if ticket.tare_kg is not None else "",
        "net_kg": f"{ticket.net_kg:.0f}" if ticket.net_kg is not None else "",
        "qty": f"{ticket.qty}" if ticket.qty is not None else "",
        "unit_id": str(ticket.unit_id or ""),
        "unit_price": f"{ticket.unit_price:.2f}" if ticket.unit_price is not None else "",
        "total": f"{ticket.total:.2f}" if ticket.total is not None else "",
        "dont_invoice": "on" if ticket.dont_invoice else "",
    }


def _form_value(form, key: str) -> str:
    return str(form.get(key, "")).strip()


def _first_non_empty_form_value(form, key: str) -> str:
    if hasattr(form, "getlist"):
        values = form.getlist(key)
        for value in values:
            text = str(value).strip()
            if text:
                return text
        if values:
            return str(values[-1]).strip()
    return _form_value(form, key)


def _normalize_number(value: str) -> str:
    return str(value).replace(",", "").strip()


def _normalize_ewc_digits(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))[:6]


def _format_ewc_code_display(code_6: str | None) -> str:
    digits = _normalize_ewc_digits(code_6)
    if len(digits) != 6:
        return digits
    return f"{digits[0:2]} {digits[2:4]} {digits[4:6]}"


def _parse_ewc_code_value(raw: str, errors: list[str]) -> str | None:
    normalized = _normalize_ewc_digits(raw)
    if not raw:
        return None
    if len(normalized) != 6:
        errors.append("EWC code must be 6 digits (for example, 01 01 01).")
        return None
    return normalized


def _parse_weight_value(raw: str, label: str, errors: list[str]) -> float | None:
    normalized = _normalize_number(raw)
    if not normalized:
        return None
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if value < 0:
        errors.append(f"{label} must be 0 or greater.")
        return None
    if value > WEIGHT_MAX_KG:
        errors.append(f"{label} must be {WEIGHT_MAX_KG:.0f} kg or less.")
        return None
    value = value.quantize(WEIGHT_QUANTIZE, rounding=ROUND_HALF_UP)
    return float(value)


def _apply_ticket_defaults(db: Session, payload: dict) -> None:
    vehicle = db.get(Vehicle, payload["vehicle_id"]) if payload.get("vehicle_id") else None
    warnings = payload.setdefault("warnings", [])

    if payload["customer_id"] is None and vehicle:
        candidate_customer_id = vehicle.default_customer_id or vehicle.owner_customer_id
        if candidate_customer_id:
            customer = db.get(Customer, candidate_customer_id)
            if customer:
                payload["customer_id"] = customer.id
                payload["form"]["customer_id"] = str(customer.id)
                if customer.on_stop:
                    warning = (
                        "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
                    )
                    if warning not in warnings:
                        warnings.append(warning)

    if payload.get("haulier_id") is None and vehicle and vehicle.default_haulier_id:
        default_haulier = db.get(Haulier, vehicle.default_haulier_id)
        if default_haulier and default_haulier.is_active:
            payload["haulier_id"] = default_haulier.id
            payload["form"]["haulier_id"] = str(default_haulier.id)

    if (
        vehicle
        and (payload.get("tare_kg") is None or payload.get("tare_kg") <= 0)
        and vehicle.default_tare_kg is not None
    ):
        default_tare_kg = float(vehicle.default_tare_kg)
        if default_tare_kg > 0:
            payload["tare_kg"] = default_tare_kg
            payload["form"]["tare_kg"] = f"{default_tare_kg:.0f}"
            gross_kg = payload.get("gross_kg")
            if gross_kg is not None:
                payload["net_kg"] = gross_kg - default_tare_kg
                payload["form"]["net_kg"] = f"{payload['net_kg']:.0f}"

    if payload.get("unit_price_raw") in ("", None) or payload.get("unit_price") is None:
        product_id = payload.get("product_id")
        if product_id:
            product = db.get(Product, product_id)
            if product and product.unit_price is not None:
                payload["unit_price"] = product.unit_price
                logger.info(
                    "Defaulted unit_price from product_id=%s to %s",
                    product_id,
                    product.unit_price,
                )

    if payload.get("qty") is not None and payload.get("unit_price") is not None:
        payload["total"] = Decimal(str(payload["qty"])) * payload["unit_price"]


def _product_unit_type(product: Product | None) -> str:
    unit = getattr(product, "unit", None) if product is not None else None
    unit_type = getattr(unit, "unit_type", None) if unit is not None else None
    return str(unit_type or "").upper()


def _coerce_qty_for_weight_product(payload: dict, product: Product | None) -> None:
    if _product_unit_type(product) != "WEIGHT":
        return
    payload["qty"] = None
    payload["total"] = None
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["qty"] = ""
        form_data["total"] = ""


def _coerce_weights_for_count_product(payload: dict, product: Product | None) -> None:
    if _product_unit_type(product) != "COUNT":
        return
    payload["gross_kg"] = None
    payload["tare_kg"] = None
    payload["net_kg"] = None
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["gross_kg"] = ""
        form_data["tare_kg"] = ""
        form_data["net_kg"] = ""
        form_data["readout_kg"] = ""


def _coerce_mode_fields(payload: dict, product: Product | None) -> None:
    _coerce_qty_for_weight_product(payload, product)
    _coerce_weights_for_count_product(payload, product)


def _apply_destination_default(
    ticket: Ticket, payload: dict, product: Product | None
) -> None:
    if payload.get("destination_id") is not None:
        return
    if ticket.destination_id is not None:
        return
    if not product or not product.default_destination_id:
        return
    payload["destination_id"] = product.default_destination_id
    payload["form"]["destination_id"] = str(product.default_destination_id)


def _apply_ticket_pricing(
    ticket: Ticket, payload: dict, product: Product | None
) -> dict:
    qty = payload.get("qty")
    unit_price = payload.get("unit_price")
    has_qty = qty is not None and qty > 0
    has_weights = (
        payload.get("gross_kg") is not None and payload.get("tare_kg") is not None
    )
    unit = getattr(product, "unit", None) if product else None

    ticket.total = None
    ticket.pricing_basis = None
    pricing_info = {"basis": None, "billable_qty": None, "net_kg": None}
    if (
        product is not None
        and product.sales_only
        and _is_waste_transaction(payload.get("transaction_type"))
    ):
        return pricing_info

    if has_qty and unit_price is not None:
        ticket.total = Decimal(str(qty)) * unit_price
        ticket.pricing_basis = "QTY"
        pricing_info["basis"] = "QTY"
        pricing_info["billable_qty"] = qty
        pricing_info["net_kg"] = payload.get("net_kg")

    if (
        pricing_info["basis"] != "QTY"
        and has_weights
        and unit_price is not None
        and unit
        and unit.unit_type == "WEIGHT"
    ):
        net_kg = payload["gross_kg"] - payload["tare_kg"]
        ticket.net_kg = net_kg
        unit_name = (unit.name or "").strip().lower()
        billable = None
        if unit_name in ("tonne", "tonnes"):
            billable = Decimal(str(net_kg)) / Decimal("1000")
            billable = billable.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        elif unit_name == "kg":
            billable = Decimal(str(net_kg)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        else:
            billable = None
            ticket.total = None
            ticket.pricing_basis = None
        if billable is not None:
            ticket.total = billable * unit_price
            ticket.pricing_basis = "WEIGHT"
            pricing_info["basis"] = "WEIGHT"
            pricing_info["billable_qty"] = billable
            pricing_info["net_kg"] = net_kg
    return pricing_info


def _validate_product_ewc(payload: dict, db: Session) -> Product | None:
    product_id = payload.get("product_id")
    if not product_id:
        return None
    product = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit), joinedload(Product.ewc_code))
            .where(Product.id == product_id)
        )
        .scalars()
        .first()
    )
    if not product:
        payload["errors"].append("Product not found.")
        return None
    if not _product_has_active_unit(product):
        payload["errors"].append(INACTIVE_PRODUCT_UNIT_ERROR)
        return None
    if _is_waste_transaction(payload.get("transaction_type")) and product.sales_only:
        payload["errors"].append(SALES_ONLY_WASTE_ERROR)
        return None
    return product


def _product_has_active_unit(product: Product | None) -> bool:
    if product is None:
        return False
    unit = product.unit
    return bool(unit and unit.is_active)


def _product_default_ewc(product: Product | None) -> EwcCode | None:
    if not product or not product.ewc_code_id:
        return None
    return product.ewc_code


def _sync_form_product_ewc_defaults(payload: dict, product: Product | None) -> None:
    default_ewc = _product_default_ewc(product)
    default_code_6 = _normalize_ewc_digits(default_ewc.code_6 if default_ewc else "")
    default_display = (
        default_ewc.code_display
        if default_ewc and default_ewc.code_display
        else _format_ewc_code_display(default_code_6)
    )
    payload["ewc_product_default_code_6"] = default_code_6
    payload["ewc_product_default_display"] = default_display or ""
    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["ewc_product_default_code_6"] = default_code_6
        form_data["ewc_product_default_display"] = default_display or ""


def _build_ewc_snapshot(db: Session, code_6: str | None) -> dict[str, object]:
    normalized = _normalize_ewc_digits(code_6)
    if not normalized:
        return {
            "ewc_code_6": None,
            "ewc_code_display": None,
            "ewc_description": None,
            "ewc_hazardous": None,
        }

    ewc = (
        db.execute(select(EwcCode).where(EwcCode.code_6 == normalized))
        .scalars()
        .first()
    )
    if ewc:
        return {
            "ewc_code_6": ewc.code_6,
            "ewc_code_display": ewc.code_display,
            "ewc_description": ewc.description,
            "ewc_hazardous": ewc.hazardous,
        }

    return {
        "ewc_code_6": normalized,
        "ewc_code_display": _format_ewc_code_display(normalized),
        "ewc_description": None,
        "ewc_hazardous": None,
    }


def _resolve_ticket_ewc_snapshot(
    payload: dict, db: Session, product: Product | None
) -> dict[str, object]:
    _sync_form_product_ewc_defaults(payload, product)

    is_waste_tx = _is_waste_transaction(payload.get("transaction_type"))
    entered_code_6 = _normalize_ewc_digits(payload.get("ewc_code_6"))
    auto_code_6 = _normalize_ewc_digits(payload.get("ewc_auto_code_6"))
    manual_override = bool(payload.get("ewc_manual_override"))
    default_ewc = _product_default_ewc(product)
    default_code_6 = _normalize_ewc_digits(default_ewc.code_6 if default_ewc else "")

    resolved_code_6 = entered_code_6
    if is_waste_tx and default_code_6:
        should_fill_blank = not entered_code_6
        should_update_previous_auto = (
            not manual_override
            and bool(auto_code_6)
            and entered_code_6 == auto_code_6
            and auto_code_6 != default_code_6
        )
        should_sync_current_default = (
            not manual_override and entered_code_6 == default_code_6
        )
        if should_fill_blank or should_update_previous_auto or should_sync_current_default:
            resolved_code_6 = default_code_6
            manual_override = False

    snapshot = _build_ewc_snapshot(db, resolved_code_6)
    resolved_code_6 = _normalize_ewc_digits(snapshot.get("ewc_code_6"))

    if not resolved_code_6:
        manual_override = False
    elif default_code_6 and resolved_code_6 != default_code_6:
        manual_override = True
    elif not default_code_6:
        manual_override = True
    else:
        manual_override = False

    snapshot["ewc_manual_override"] = manual_override

    payload["ewc_code_6"] = resolved_code_6 or None
    payload["ewc_manual_override"] = manual_override
    payload["ewc_auto_code_6"] = resolved_code_6 if (resolved_code_6 and not manual_override) else ""

    form_data = payload.get("form")
    if isinstance(form_data, dict):
        form_data["ewc_code"] = str(snapshot.get("ewc_code_display") or "")
        form_data["ewc_manual_override"] = "1" if manual_override else "0"
        form_data["ewc_auto_code_6"] = _format_ewc_code_display(
            payload.get("ewc_auto_code_6")
        )

    return snapshot


def _validate_waste_ewc_on_complete(payload: dict, ewc_snapshot: dict[str, object]) -> None:
    if not _is_waste_transaction(payload.get("transaction_type")):
        return
    if not _normalize_ewc_digits(ewc_snapshot.get("ewc_code_6")):
        payload["errors"].append("EWC code is required to complete a waste ticket.")


def _validate_required_on_complete(payload: dict) -> None:
    transaction_type = payload.get("transaction_type")
    is_waste_tx = _is_waste_transaction(transaction_type)
    is_sale = transaction_type == TransactionTypeEnum.SALE.value

    if not payload.get("direction"):
        payload["errors"].append("Direction is required.")
    if not payload.get("transaction_type"):
        payload["errors"].append("Transaction type is required.")

    if is_waste_tx:
        if not payload.get("customer_id"):
            payload["errors"].append(
                "Customer is required to complete a waste ticket."
            )
        if not payload.get("product_id"):
            payload["errors"].append("Product is required to complete a ticket.")
        if not payload.get("destination_id"):
            payload["errors"].append(
                "Destination is required to complete a waste ticket."
            )
    elif is_sale:
        if not payload.get("customer_id"):
            payload["errors"].append(
                "Customer is required to complete a sale ticket."
            )
        if not payload.get("product_id"):
            payload["errors"].append("Product is required to complete a ticket.")


def _is_waste_transaction(transaction_type: str | None) -> bool:
    return transaction_type in (
        TransactionTypeEnum.WASTEIN.value,
        TransactionTypeEnum.WASTEOUT.value,
    )


def _apply_ticket_ewc_snapshot(ticket: Ticket, snapshot: dict[str, object]) -> None:
    ticket.ewc_code_6 = snapshot.get("ewc_code_6")
    ticket.ewc_code_display = snapshot.get("ewc_code_display")
    ticket.ewc_description = snapshot.get("ewc_description")
    ticket.ewc_hazardous = snapshot.get("ewc_hazardous")
    ticket.ewc_manual_override = bool(snapshot.get("ewc_manual_override"))


def _validate_carrier_licence(payload: dict, db: Session) -> Haulier | None:
    haulier_id = payload.get("haulier_id")
    if not haulier_id:
        return None
    haulier = db.get(Haulier, haulier_id)
    return haulier


def _apply_carrier_licence_snapshot(
    ticket: Ticket, haulier: Haulier | None
) -> None:
    ticket.carrier_licence_number = (
        haulier.carrier_licence_number if haulier else None
    )


def _format_customer_address(customer: Customer) -> str | None:
    parts = [
        customer.address_line1,
        customer.address_line2,
        customer.city,
        customer.postcode,
        customer.country,
    ]
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _normalize_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _compose_manual_waste_producer_address(payload: dict) -> str | None:
    parts = [
        payload.get("waste_producer_address_line_1"),
        payload.get("waste_producer_address_line_2"),
        payload.get("waste_producer_address_line_3"),
        payload.get("waste_producer_postcode"),
    ]
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _split_waste_producer_address(address: str | None) -> tuple[str, str, str, str]:
    if not address:
        return ("", "", "", "")
    normalized = address.replace("\r", "\n")
    if "\n" in normalized:
        parts = [part.strip() for part in normalized.split("\n") if part.strip()]
    else:
        parts = [part.strip() for part in normalized.split(",") if part.strip()]
    line_1 = parts[0] if len(parts) > 0 else ""
    line_2 = parts[1] if len(parts) > 1 else ""
    line_3 = parts[2] if len(parts) > 2 else ""
    postcode = parts[3] if len(parts) > 3 else ""
    return (line_1, line_2, line_3, postcode)


def _resolve_waste_producer_snapshot(
    payload: dict,
    db: Session,
    *,
    require_customer_when_same: bool = False,
    require_manual_name: bool = False,
) -> None:
    same_as_customer = bool(payload.get("waste_producer_same_as_customer"))
    payload["waste_producer_source"] = (
        WasteProducerSourceEnum.CUSTOMER.value
        if same_as_customer
        else WasteProducerSourceEnum.MANUAL.value
    )

    if same_as_customer:
        customer_id = payload.get("customer_id")
        if not customer_id:
            if require_customer_when_same:
                payload["errors"].append(
                    "Waste producer is set to same as customer - select a customer."
                )
            payload["waste_producer_name_snapshot"] = None
            payload["waste_producer_address_snapshot"] = None
            return
        customer = db.get(Customer, customer_id)
        if not customer:
            payload["errors"].append("Waste producer customer not found.")
            payload["waste_producer_name_snapshot"] = None
            payload["waste_producer_address_snapshot"] = None
            return
        payload["waste_producer_name_snapshot"] = _normalize_optional_text(customer.name)
        payload["waste_producer_address_snapshot"] = _format_customer_address(customer)
        return

    producer_name = _normalize_optional_text(payload.get("waste_producer_name"))
    if require_manual_name and not producer_name:
        payload["errors"].append(
            "Enter waste producer name or tick 'Waste producer same as customer'."
        )
    payload["waste_producer_name_snapshot"] = producer_name
    payload["waste_producer_address_snapshot"] = _compose_manual_waste_producer_address(
        payload
    )


def _apply_waste_producer_snapshot(ticket: Ticket, payload: dict) -> None:
    ticket.waste_producer_name = payload.get("waste_producer_name_snapshot")
    ticket.waste_producer_address = payload.get("waste_producer_address_snapshot")

def _net_negative(ticket: Ticket) -> bool:
    if ticket.gross_kg is None or ticket.tare_kg is None:
        return False
    try:
        return Decimal(str(ticket.gross_kg)) - Decimal(str(ticket.tare_kg)) < 0
    except (InvalidOperation, ValueError):
        return False


def _net_negative_values(gross_kg: float | None, tare_kg: float | None) -> bool:
    if gross_kg is None or tare_kg is None:
        return False
    try:
        return Decimal(str(gross_kg)) - Decimal(str(tare_kg)) < 0
    except (InvalidOperation, ValueError):
        return False


def _direction_transaction_warning(direction, transaction_type) -> bool:
    def as_value(value) -> str:
        if value is None:
            return ""
        return value.value if hasattr(value, "value") else str(value)

    direction_value = as_value(direction)
    transaction_value = as_value(transaction_type)
    if not direction_value or not transaction_value:
        return False
    if direction_value == "OUTWARD" and transaction_value in ("WASTEIN", "GOODSIN"):
        return True
    if direction_value == "INWARD" and transaction_value in (
        "WASTEOUT",
        "GOODSOUT",
        "SALE",
    ):
        return True
    return False


def _render_vehicle_suggestions(
    request: Request,
    ticket: Ticket,
    vehicle: Vehicle | None,
    default_customer: Customer | None,
    default_haulier: Haulier | None,
    default_driver: Driver | None = None,
    *,
    suggested_tare_kg: float | None = None,
    ticket_vehicle_reg: str | None = None,
    current_customer_id: int | None = None,
    current_haulier_id: int | None = None,
    current_driver_id: int | None = None,
    oob_customer_options: list[tuple[str, str]] | None = None,
    oob_haulier_options: list[tuple[str, str]] | None = None,
    oob_driver_options: list[tuple[str, str]] | None = None,
    oob_weights_form: dict | None = None,
    oob_tare_auto_hint: str | None = None,
    oob_status_warnings: list[str] | None = None,
    show_panel: bool = True,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    def _to_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    has_vehicle_match = (
        vehicle is not None
        and (ticket.vehicle_id is None or ticket.vehicle_id == vehicle.id)
    )

    selected_customer_id = (
        ticket.customer_id if current_customer_id is None else current_customer_id
    )
    selected_haulier_id = (
        ticket.haulier_id if current_haulier_id is None else current_haulier_id
    )
    selected_driver_id = (
        ticket.driver_id if current_driver_id is None else current_driver_id
    )
    can_apply_customer = (
        has_vehicle_match
        and default_customer is not None
        and selected_customer_id is None
    )
    can_apply_haulier = (
        has_vehicle_match
        and default_haulier is not None
        and default_haulier.is_active
        and selected_haulier_id is None
    )
    can_apply_driver = (
        has_vehicle_match
        and default_driver is not None
        and default_driver.is_active
        and selected_driver_id is None
    )
    suggested_tare_value = _to_decimal(suggested_tare_kg)
    current_tare_value = _to_decimal(ticket.tare_kg)
    can_apply_tare = (
        has_vehicle_match
        and suggested_tare_value is not None
        and suggested_tare_value > 0
        and (current_tare_value is None or current_tare_value != suggested_tare_value)
    )
    can_apply_vehicle = vehicle is not None and ticket.vehicle_id != vehicle.id
    can_apply_suggestion = (
        vehicle is not None
        and (can_apply_vehicle or can_apply_customer or can_apply_haulier or can_apply_driver or can_apply_tare)
    )
    should_show_panel = show_panel and (can_apply_suggestion or bool(errors))
    return templates.TemplateResponse(request, 
        "tickets/_vehicle_suggestions.html",
        {
            "request": request,
            "ticket": ticket,
            "vehicle": vehicle,
            "default_customer": default_customer,
            "default_haulier": default_haulier,
            "default_driver": default_driver,
            "suggested_tare_kg": suggested_tare_kg,
            "show_panel": should_show_panel,
            "vehicle_mismatch": (
                vehicle is not None
                and ticket.vehicle_id is not None
                and ticket.vehicle_id != vehicle.id
            ),
            "ticket_vehicle_reg": ticket_vehicle_reg or "",
            "oob_customer_options": oob_customer_options or [],
            "oob_haulier_options": oob_haulier_options or [],
            "oob_driver_options": oob_driver_options or [],
            "oob_customer_selected": str(selected_customer_id or ""),
            "oob_haulier_selected": str(selected_haulier_id or ""),
            "oob_driver_selected": str(selected_driver_id or ""),
            "can_apply_suggestion": can_apply_suggestion,
            "oob_weights_form": oob_weights_form,
            "oob_tare_auto_hint": oob_tare_auto_hint,
            "oob_status_warnings": oob_status_warnings,
            "warnings": warnings or [],
            "errors": errors or [],
            "is_open": _is_open_ticket(ticket),
        },
        status_code=status_code,
    )


def _render_ticket_edit(
    request: Request,
    ticket: Ticket,
    db: Session,
    *,
    errors: list[str],
    warnings: list[str] | None = None,
    form: dict | None = None,
    vehicle_reg: str | None = None,
    weight_warning: bool | None = None,
    direction_warning: bool | None = None,
    status_code: int = 400,
) -> HTMLResponse:
    _ensure_ticket_void_reasons(db)
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    ticket_void, ticket_void_reason = _latest_ticket_void_with_reason(db, ticket.id)
    selected_customer_id = None
    if form and form.get("customer_id"):
        selected_customer_id = _parse_int(str(form.get("customer_id")))
    if selected_customer_id is None:
        selected_customer_id = ticket.customer_id
    selected_haulier_id = None
    if form and form.get("haulier_id"):
        selected_haulier_id = _parse_int(str(form.get("haulier_id")))
    if selected_haulier_id is None:
        selected_haulier_id = ticket.haulier_id
    stop_blockers = _on_stop_blockers(db, selected_customer_id, selected_haulier_id)
    product_id = None
    if form and form.get("product_id"):
        product_id = _parse_int(str(form.get("product_id")))
    if product_id is None:
        product_id = ticket.product_id
    selected_transaction_type = None
    if form and form.get("transaction_type"):
        selected_transaction_type = str(form.get("transaction_type"))
    if selected_transaction_type is None:
        selected_transaction_type = _enum_value_or_text(ticket.transaction_type)
    default_destination_id = None
    unit_type = None
    if product_id:
        product = db.get(Product, product_id)
        if product:
            default_destination_id = product.default_destination_id
            unit_type = product.unit.unit_type if product.unit else None
    options = _load_ticket_options_with_enums(
        db,
        transaction_type=selected_transaction_type,
        selected_product_id=product_id,
    )
    resolved_warnings = list(warnings or [])
    product_warning = _sales_only_selected_product_warning(
        db, selected_transaction_type, product_id
    )
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": errors,
            "warnings": resolved_warnings,
            "product_usage_warning": product_warning,
            "saved": False,
            "completed": False,
            "ticket": ticket,
            "ticket_void": ticket_void,
            "ticket_void_reason": ticket_void_reason,
            "invoice": invoice,
            "is_admin": False,
            "is_open": _is_open_ticket(ticket),
            "is_locked": _is_ticket_locked(ticket),
            "can_void": _can_void_ticket(ticket),
            "locked_message": _ticket_locked_message(ticket)
            if _is_ticket_locked(ticket)
            else "",
            "stop_blocked": bool(stop_blockers),
            "stop_banner_message": _on_stop_banner_message(stop_blockers),
            "weight_warning": _net_negative(ticket)
            if weight_warning is None
            else weight_warning,
            "direction_warning": _direction_transaction_warning(
                ticket.direction, ticket.transaction_type
            )
            if direction_warning is None
            else direction_warning,
            "form": form or _ticket_to_form(ticket),
            "vehicle_reg": vehicle_reg if vehicle_reg is not None else _ticket_vehicle_reg(db, ticket),
            "default_destination_id": default_destination_id,
            "unit_type": unit_type,
            "options": options,
            "product_unit_meta": _load_product_unit_meta(db),
            "enums": _ticket_enums(),
            **_active_lookup_options(ticket, db),
        },
        status_code=status_code,
    )


def _render_weights_partial(
    request: Request, ticket: Ticket, errors: list[str], status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "tickets/_weights_block.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": errors,
            "is_admin": False,
            "is_open": _is_open_ticket(ticket),
            "form": _ticket_to_form(ticket),
            "show_weight_errors": True,
        },
        status_code=status_code,
    )


def _weights_form_from_values(
    ticket: Ticket,
    gross_raw: str,
    tare_raw: str,
    readout_raw: str,
    net_value: float | None,
) -> dict:
    form_data = _ticket_to_form(ticket)
    form_data["gross_kg"] = gross_raw
    form_data["tare_kg"] = tare_raw
    form_data["readout_kg"] = readout_raw
    form_data["net_kg"] = f"{net_value:.0f}" if net_value is not None else ""
    return form_data


def _ticket_enums() -> dict[str, list[str]]:
    return {
        "directions": [value.value for value in DirectionEnum],
        "transaction_types": [value.value for value in TransactionTypeEnum],
        "statuses": [value.value for value in TicketStatusEnum],
    }


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    normalized = _normalize_number(value)
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _parse_decimal(value: str) -> Decimal | None:
    normalized = _normalize_number(value)
    if not normalized:
        return None
    try:
        return Decimal(str(normalized))
    except (InvalidOperation, ValueError):
        return None

