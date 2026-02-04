from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    Haulier,
    Invoice,
    Product,
    Ticket,
    TicketVoid,
    TicketStatusEnum,
    TransactionTypeEnum,
    Vehicle,
    VoidReason,
    Yard,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

LOCKED_STATUSES = {TicketStatusEnum.COMPLETE.value, TicketStatusEnum.VOID.value}
NEW_TICKET_DEDUP_SECONDS = 5
WEIGHT_MAX_KG = Decimal("1000000")
WEIGHT_QUANTIZE = Decimal("1")


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
        dont_invoice=False,
        paid=False,
    )
    db.add(ticket)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket.id}", status_code=303)


@router.get("/tickets/product-defaults", response_class=HTMLResponse)
def ticket_product_defaults(
    request: Request,
    product_id: int | None = Query(None),
    unit_price: str | None = Query(None),
    transaction_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not product_id:
        return HTMLResponse("", status_code=204)

    product = db.get(Product, product_id)
    if not product:
        return HTMLResponse("", status_code=204)

    current_unit_price = unit_price.strip() if unit_price else ""

    unit_price_value = (
        current_unit_price if current_unit_price != "" else product.unit_price
    )

    ewc = product.ewc_code if product else None
    unit_name = product.unit.name if product and product.unit else ""
    unit_type = product.unit.unit_type if product and product.unit else ""
    return templates.TemplateResponse(request, 
        "tickets/_product_defaults.html",
        {
            "request": request,
            "unit_price": f"{unit_price_value:.2f}"
            if unit_price_value is not None
            else "",
            "ewc_code_display": ewc.code_display if ewc else None,
            "ewc_description": ewc.description if ewc else None,
            "ewc_hazardous": bool(ewc.hazardous) if ewc else False,
            "default_destination_id": product.default_destination_id,
            "unit_type": unit_type,
            "unit_name": unit_name,
            "transaction_type": transaction_type or "",
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


def _load_ticket_options_with_enums(db: Session) -> dict:
    return {
        **_load_ticket_options(db),
        "directions": _ticket_direction_options(),
        "transaction_types": _ticket_transaction_type_options(),
    }


@router.get("/tickets/vehicle-suggest", response_class=HTMLResponse)
def tickets_vehicle_suggest(
    request: Request,
    reg: str | None = Query(None),
    ticket_id: int | None = Query(None),
    walk_in: str | None = Query(None),
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

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
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


def _load_ticket_options(db: Session | None) -> dict[str, list[tuple[int, str]]]:
    if db is None:
        return {key: [] for key in _option_keys()}

    def as_options(rows, label_fn):
        return [(str(row.id), label_fn(row)) for row in rows]

    return {
        "customers": as_options(
            db.execute(select(Customer).order_by(Customer.name)).scalars().all(),
            lambda row: row.name,
        ),
        "vehicles": as_options(
            db.execute(select(Vehicle).order_by(Vehicle.registration)).scalars().all(),
            lambda row: row.registration,
        ),
        "products": as_options(
            db.execute(select(Product).order_by(Product.description)).scalars().all(),
            lambda row: row.description,
        ),
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
        "yards": as_options(
            db.execute(select(Yard).order_by(Yard.code)).scalars().all(),
            lambda row: row.code,
        ),
        "areas": as_options(
            db.execute(select(Area).order_by(Area.code)).scalars().all(),
            lambda row: row.code,
        ),
        "void_reasons": as_options(
            db.execute(select(VoidReason).order_by(VoidReason.code)).scalars().all(),
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
        "yards",
        "areas",
        "void_reasons",
    ]


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


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
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

    is_admin = True
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    options = _load_ticket_options_with_enums(db)
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": [],
            "saved": request.query_params.get("saved") == "1",
            "completed": request.query_params.get("completed") == "1",
            "ticket": ticket,
            "invoice": invoice,
            "is_admin": is_admin,
            "is_open": _is_open_ticket(ticket),
            "weight_warning": _net_negative(ticket),
            "direction_warning": _direction_transaction_warning(
                ticket.direction, ticket.transaction_type
            ),
            "form": _ticket_to_form(ticket),
            "vehicle_reg": _ticket_vehicle_reg(db, ticket),
            "options": options,
            "enums": _ticket_enums(),
            **_active_lookup_options(ticket, db),
        },
    )


@router.post("/tickets/{ticket_id}", response_class=HTMLResponse)
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

    if _is_ticket_locked(ticket):
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Ticket is locked."],
            status_code=403,
        )

    payload = _parse_ticket_form(
        form, current_status=ticket.status.value if ticket.status else None
    )
    payload["form"]["direction"] = payload["direction"] or ""
    payload["form"]["transaction_type"] = payload["transaction_type"] or ""
    if payload["vehicle_id"] is None and ticket.vehicle_id is not None:
        payload["vehicle_id"] = ticket.vehicle_id
        payload["form"]["vehicle_id"] = str(ticket.vehicle_id)

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
        _apply_destination_default(ticket, payload, product)
        haulier = _validate_carrier_licence(payload, db)
        producer_override = _validate_waste_producer_customer(payload, db)
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
        has_qty = payload.get("qty") is not None and payload.get("qty") > 0
        has_gross = payload.get("gross_kg") is not None
        has_tare = payload.get("tare_kg") is not None
        if is_waste_tx and has_gross and not has_tare:
            vehicle = (
                db.get(Vehicle, payload["vehicle_id"])
                if payload.get("vehicle_id")
                else None
            )
            if vehicle and vehicle.default_tare_kg is not None:
                payload["tare_kg"] = vehicle.default_tare_kg
                payload["form"]["tare_kg"] = f"{vehicle.default_tare_kg:.0f}"
                payload["net_kg"] = payload["gross_kg"] - payload["tare_kg"]
                payload["form"]["net_kg"] = f"{payload['net_kg']:.0f}"
                has_tare = True
        has_weights = has_gross and has_tare
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
            has_weights
            and _net_negative_values(payload["gross_kg"], payload["tare_kg"])
        ):
            payload["errors"].append(
                "Net weight cannot be negative. Use Swap Weights."
            )

        _validate_product_has_ewc_on_complete(payload, product)

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
                form=payload["form"],
                vehicle_reg=payload["vehicle_reg_text"],
                weight_warning=weight_warning,
                direction_warning=direction_warning,
                status_code=400,
            )

        _apply_ticket_updates(ticket, payload)
        pricing_info = _apply_ticket_pricing(ticket, payload, product)
        if is_waste_tx:
            _apply_ticket_ewc_snapshot(ticket, product)
        else:
            _apply_ticket_ewc_snapshot(ticket, None)
        _apply_carrier_licence_snapshot(ticket, haulier)
        producer = producer_override
        if producer is None and payload.get("customer_id"):
            producer = db.get(Customer, payload["customer_id"])
        _apply_waste_producer_snapshot(ticket, producer)
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
        reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
        note = str(form.get("void_note", "")).strip()
        errors = []
        if not reason_id:
            errors.append("Void reason is required.")
        reason = db.get(VoidReason, reason_id) if reason_id else None
        if reason and reason.code == "OTHER" and not note:
            errors.append("Void note is required for 'Other'.")
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
                note=note,
                voided_at=utcnow(),
                voided_by="admin",
            )
        )
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)

    ticket.direction = payload["direction"]
    ticket.transaction_type = payload["transaction_type"]
    direction_warning = _direction_transaction_warning(
        payload["direction"], payload["transaction_type"]
    )
    weight_warning = _net_negative_values(payload["gross_kg"], payload["tare_kg"])
    lookup_errors = _validate_lookup_fields(ticket, payload, db)
    payload["errors"].extend(lookup_errors)
    payload["errors"].extend(
        _validate_weighing_order(
            payload["direction"], payload["gross_kg"], payload["tare_kg"]
        )
    )
    _apply_ticket_defaults(db, payload)
    product = _validate_product_ewc(payload, db)
    _apply_destination_default(ticket, payload, product)
    haulier = _validate_carrier_licence(payload, db)
    producer_override = _validate_waste_producer_customer(payload, db)
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
            form=payload["form"],
            vehicle_reg=payload["vehicle_reg_text"],
            weight_warning=weight_warning,
            direction_warning=direction_warning,
            status_code=400,
        )

    _apply_ticket_updates(ticket, payload)
    _apply_ticket_pricing(ticket, payload, product)
    if is_waste_tx:
        _apply_ticket_ewc_snapshot(ticket, product)
    else:
        _apply_ticket_ewc_snapshot(ticket, None)
    _apply_carrier_licence_snapshot(ticket, haulier)
    producer = producer_override
    if producer is None and payload.get("customer_id"):
        producer = db.get(Customer, payload["customer_id"])
    _apply_waste_producer_snapshot(ticket, producer)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}?saved=1", status_code=303)


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
            errors=["Ticket is locked."],
            status_code=403,
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
        return _render_vehicle_suggestions(
            request,
            ticket,
            vehicle,
            default_customer,
            default_haulier,
            ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
            errors=["Default customer is on stop."],
            status_code=400,
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
        show_panel=show_panel,
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
            errors=["Ticket is locked."],
            status_code=403,
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

    ticket.haulier_id = default_haulier.id
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
        show_panel=show_panel,
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
            errors=["Ticket is locked."],
            status_code=403,
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
    updates = 0

    if ticket.customer_id:
        errors.append("Customer already set.")
    elif not default_customer:
        errors.append("Default customer not configured for this vehicle.")
    elif default_customer.on_stop:
        errors.append("Default customer is on stop.")
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
            show_panel=show_panel,
            errors=errors,
        )

    return _render_vehicle_suggestions(
        request,
        ticket,
        vehicle,
        default_customer,
        default_haulier,
        ticket_vehicle_reg=_ticket_vehicle_reg(db, ticket),
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
            errors=["Ticket is locked."],
            status_code=403,
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
            request, ticket, errors=["Ticket is locked."], status_code=403
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
            request, ticket, errors=["Ticket is locked."], status_code=403
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

    readout_raw = _form_value(form, "readout_kg")
    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_value = _parse_float(gross_raw)
    tare_value = _parse_float(tare_raw)

    errors: list[str] = []
    readout_value = _parse_weight_value(readout_raw, "Readout weight", errors)
    read_confirm = False

    if readout_value is None:
        if not errors:
            errors.append("Please input a weight")
    elif gross_value is None:
        gross_value = readout_value
        gross_raw = readout_raw
    elif tare_value is None:
        tare_value = readout_value
        tare_raw = readout_raw
    else:
        read_confirm = True

    net_value = (
        gross_value - tare_value
        if gross_value is not None and tare_value is not None
        else None
    )
    form_data = _weights_form_from_values(
        ticket,
        gross_raw=gross_raw,
        tare_raw=tare_raw,
        readout_raw=readout_raw,
        net_value=net_value,
    )

    return templates.TemplateResponse(request, 
        "tickets/_weights_block.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": errors,
            "is_admin": True,
            "form": form_data,
            "show_weight_errors": True,
            "read_confirm": read_confirm,
        },
        status_code=400 if errors else 200,
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

    read_target = _form_value(form, "read_target")
    readout_raw = _form_value(form, "readout_kg")
    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_value = _parse_float(gross_raw)
    tare_value = _parse_float(tare_raw)

    errors: list[str] = []
    readout_value = _parse_weight_value(readout_raw, "Readout weight", errors)

    if readout_value is None:
        if not errors:
            errors.append("Please input a weight")
    elif read_target == "gross":
        gross_value = readout_value
        gross_raw = readout_raw
    elif read_target == "tare":
        tare_value = readout_value
        tare_raw = readout_raw
    else:
        errors.append("Select a weight to overwrite.")

    net_value = (
        gross_value - tare_value
        if gross_value is not None and tare_value is not None
        else None
    )
    form_data = _weights_form_from_values(
        ticket,
        gross_raw=gross_raw,
        tare_raw=tare_raw,
        readout_raw=readout_raw,
        net_value=net_value,
    )

    return templates.TemplateResponse(request, 
        "tickets/_weights_block.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": errors,
            "is_admin": True,
            "form": form_data,
            "show_weight_errors": True,
        },
        status_code=400 if errors else 200,
    )


@router.post("/tickets/swap-weights-preview", response_class=HTMLResponse)
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

    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    swapped_gross = tare_raw
    swapped_tare = gross_raw

    gross_value = _parse_float(swapped_gross)
    tare_value = _parse_float(swapped_tare)
    net_value = (
        gross_value - tare_value
        if gross_value is not None and tare_value is not None
        else None
    )

    form_data = _ticket_to_form(ticket)
    form_data["gross_kg"] = swapped_gross
    form_data["tare_kg"] = swapped_tare
    form_data["net_kg"] = f"{net_value:.0f}" if net_value is not None else ""

    return templates.TemplateResponse(request, 
        "tickets/_weights_preview.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": [],
            "is_admin": True,
            "form": form_data,
            "show_weight_errors": True,
            "weight_warning": _net_negative_values(gross_value, tare_value),
        },
    )


@router.post("/tickets/{ticket_id}/swap-weights", response_class=HTMLResponse)
def tickets_swap_weights(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if _is_ticket_locked(ticket):
        if request.headers.get("HX-Request") == "true":
            return _render_weights_partial(
                request, ticket, errors=["Ticket is locked."], status_code=403
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Ticket is locked."],
            status_code=403,
        )
    if ticket.gross_kg is None or ticket.tare_kg is None:
        if request.headers.get("HX-Request") == "true":
            return _render_weights_partial(
                request,
                ticket,
                errors=["Gross and tare weights are required to swap."],
                status_code=400,
            )
        return _render_ticket_edit(
            request,
            ticket,
            db,
            errors=["Gross and tare weights are required to swap."],
            status_code=400,
        )

    ticket.gross_kg, ticket.tare_kg = ticket.tare_kg, ticket.gross_kg
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = utcnow()
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, 
            "tickets/_weights_swap.html",
            {
                "request": request,
                "ticket": ticket,
                "errors": [],
                "is_admin": True,
                "form": _ticket_to_form(ticket),
                "show_weight_errors": True,
                "weight_warning": _net_negative(ticket),
            },
        )
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)


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
    ticket.yard_id = payload["yard_id"]
    ticket.area_id = payload["area_id"]
    ticket.waste_code_id = payload["waste_code_id"]
    ticket.waste_producer_customer_id = payload["waste_producer_customer_id"]
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
    product_id = _parse_int(_form_value(form, "product_id"))
    waste_producer_customer_id = _parse_int(
        _form_value(form, "waste_producer_customer_id")
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
        "product_id": _form_value(form, "product_id"),
        "haulier_id": _form_value(form, "haulier_id"),
        "driver_id": _form_value(form, "driver_id"),
        "container_id": _form_value(form, "container_id"),
        "destination_id": _form_value(form, "destination_id"),
        "yard_id": _form_value(form, "yard_id"),
        "area_id": _form_value(form, "area_id"),
        "waste_code_id": _form_value(form, "waste_code_id"),
        "waste_producer_customer_id": _form_value(form, "waste_producer_customer_id"),
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
        "waste_code_id": _parse_int(_form_value(form, "waste_code_id")),
        "waste_producer_customer_id": waste_producer_customer_id,
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
        "waste_producer_customer_id": str(ticket.waste_producer_customer_id or ""),
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


def _normalize_number(value: str) -> str:
    return str(value).replace(",", "").strip()


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
    if payload["customer_id"] is None and payload.get("vehicle_id"):
        vehicle = db.get(Vehicle, payload["vehicle_id"])
        if vehicle and vehicle.owner_customer_id:
            payload["customer_id"] = vehicle.owner_customer_id
            payload["form"]["customer_id"] = str(vehicle.owner_customer_id)

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
            .options(joinedload(Product.unit))
            .where(Product.id == product_id)
        )
        .scalars()
        .first()
    )
    if not product:
        payload["errors"].append("Product not found.")
        return None
    return product


def _validate_product_has_ewc_on_complete(
    payload: dict, product: Product | None
) -> None:
    if _is_waste_transaction(payload.get("transaction_type")):
        if product and product.ewc_code_id is None:
            payload["errors"].append(
                "Waste ticket requires an EWC code (set EWC on Product)."
            )


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


def _apply_ticket_ewc_snapshot(ticket: Ticket, product: Product | None) -> None:
    if not product or not product.ewc_code_id:
        ticket.ewc_code_6 = None
        ticket.ewc_code_display = None
        ticket.ewc_description = None
        ticket.ewc_hazardous = None
        return

    ewc = product.ewc_code
    if not ewc:
        ticket.ewc_code_6 = None
        ticket.ewc_code_display = None
        ticket.ewc_description = None
        ticket.ewc_hazardous = None
        return

    ticket.ewc_code_6 = ewc.code_6
    ticket.ewc_code_display = ewc.code_display
    ticket.ewc_description = ewc.description
    ticket.ewc_hazardous = ewc.hazardous


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


def _validate_waste_producer_customer(payload: dict, db: Session) -> Customer | None:
    producer_id = payload.get("waste_producer_customer_id")
    if not producer_id:
        return None
    producer = db.get(Customer, producer_id)
    if not producer:
        payload["errors"].append("Waste producer not found.")
        return None
    return producer


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


def _apply_waste_producer_snapshot(
    ticket: Ticket, producer: Customer | None
) -> None:
    if not producer:
        ticket.waste_producer_name = None
        ticket.waste_producer_address = None
        return
    ticket.waste_producer_name = producer.name
    ticket.waste_producer_address = _format_customer_address(producer)

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
    *,
    ticket_vehicle_reg: str | None = None,
    oob_customer_options: list[tuple[str, str]] | None = None,
    oob_haulier_options: list[tuple[str, str]] | None = None,
    show_panel: bool = True,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    has_vehicle_match = (
        vehicle is not None
        and (ticket.vehicle_id is None or ticket.vehicle_id == vehicle.id)
    )
    can_apply_customer = (
        has_vehicle_match and default_customer is not None and ticket.customer_id is None
    )
    can_apply_haulier = (
        has_vehicle_match and default_haulier is not None and ticket.haulier_id is None
    )
    return templates.TemplateResponse(request, 
        "tickets/_vehicle_suggestions.html",
        {
            "request": request,
            "ticket": ticket,
            "vehicle": vehicle,
            "default_customer": default_customer,
            "default_haulier": default_haulier,
            "show_panel": show_panel,
            "has_vehicle_match": has_vehicle_match,
            "vehicle_mismatch": (
                vehicle is not None
                and ticket.vehicle_id is not None
                and ticket.vehicle_id != vehicle.id
            ),
            "ticket_vehicle_reg": ticket_vehicle_reg or "",
            "oob_customer_options": oob_customer_options or [],
            "oob_haulier_options": oob_haulier_options or [],
            "oob_customer_selected": str(ticket.customer_id or ""),
            "oob_haulier_selected": str(ticket.haulier_id or ""),
            "can_apply_customer": can_apply_customer,
            "can_apply_haulier": can_apply_haulier,
            "can_apply_all": can_apply_customer and can_apply_haulier,
            "errors": errors or [],
        },
        status_code=status_code,
    )


def _render_ticket_edit(
    request: Request,
    ticket: Ticket,
    db: Session,
    *,
    errors: list[str],
    form: dict | None = None,
    vehicle_reg: str | None = None,
    weight_warning: bool | None = None,
    direction_warning: bool | None = None,
    status_code: int = 400,
) -> HTMLResponse:
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    product_id = None
    if form and form.get("product_id"):
        product_id = _parse_int(str(form.get("product_id")))
    if product_id is None:
        product_id = ticket.product_id
    default_destination_id = None
    unit_type = None
    if product_id:
        product = db.get(Product, product_id)
        if product:
            default_destination_id = product.default_destination_id
            unit_type = product.unit.unit_type if product.unit else None
    options = _load_ticket_options_with_enums(db)
    return templates.TemplateResponse(request, 
        "tickets/edit.html",
        {
            "request": request,
            "errors": errors,
            "saved": False,
            "completed": False,
            "ticket": ticket,
            "invoice": invoice,
            "is_admin": True,
            "is_open": _is_open_ticket(ticket),
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
            "is_admin": True,
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

