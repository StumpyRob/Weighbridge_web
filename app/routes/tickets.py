from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Area,
    Container,
    Customer,
    DirectionEnum,
    Destination,
    Driver,
    Haulier,
    Invoice,
    Licence,
    Product,
    Ticket,
    TicketVoid,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
    VoidReason,
    WasteCode,
    WasteProducer,
    Yard,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


@router.get("/tickets", response_class=HTMLResponse)
def tickets_list(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
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
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        filters.append(Ticket.datetime <= datetime.combine(date_to, time.max))
    if status:
        filters.append(Ticket.status == status)
    if direction:
        filters.append(Ticket.direction == direction)
    if transaction_type:
        filters.append(Ticket.transaction_type == transaction_type)
    if ticket_no:
        filters.append(Ticket.ticket_no.ilike(f"%{ticket_no}%"))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Ticket.ticket_no.ilike(like),
                Vehicle.registration.ilike(like),
                Customer.name.ilike(like),
            )
        )

    base_stmt = (
        select(Ticket, Customer, Vehicle, Product, Invoice)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .outerjoin(Product, Ticket.product_id == Product.id)
        .outerjoin(Invoice, Ticket.invoice_id == Invoice.id)
        .where(*filters)
    )
    count_stmt = (
        select(func.count(func.distinct(Ticket.id)))
        .select_from(Ticket)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
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

    return templates.TemplateResponse(
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
                "direction": direction or "",
                "transaction_type": transaction_type or "",
                "ticket_no": ticket_no or "",
                "q": q or "",
            },
        },
    )


@router.post("/tickets/new/quick", response_class=HTMLResponse)
def tickets_quick_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    now = datetime.utcnow()
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
    unit_id: int | None = Query(None),
    unit_price: str | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not product_id:
        return HTMLResponse("", status_code=204)

    product = db.get(Product, product_id)
    if not product:
        return HTMLResponse("", status_code=204)

    current_unit_id = unit_id
    current_unit_price = unit_price.strip() if unit_price else ""

    selected_unit_id = (
        current_unit_id if current_unit_id is not None else product.unit_id
    )
    unit_price_value = (
        current_unit_price if current_unit_price != "" else product.unit_price
    )

    units = db.execute(select(Unit).order_by(Unit.code)).scalars().all()
    return templates.TemplateResponse(
        "tickets/_pricing_defaults.html",
        {
            "request": request,
            "units": [(str(row.id), row.code) for row in units],
            "unit_id": str(selected_unit_id or ""),
            "unit_price": f"{unit_price_value:.2f}"
            if unit_price_value is not None
            else "",
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
        "tickets/_mismatch_warning.html",
        {"request": request},
    )


def _generate_ticket_no(db: Session, now: datetime | None = None) -> str:
    current_time = now or datetime.utcnow()
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
            db.execute(select(Haulier).order_by(Haulier.code)).scalars().all(),
            lambda row: row.code,
        ),
        "drivers": as_options(
            db.execute(select(Driver).order_by(Driver.name)).scalars().all(),
            lambda row: row.name,
        ),
        "containers": as_options(
            db.execute(select(Container).order_by(Container.code)).scalars().all(),
            lambda row: row.code,
        ),
        "destinations": as_options(
            db.execute(select(Destination).order_by(Destination.code)).scalars().all(),
            lambda row: row.code,
        ),
        "yards": as_options(
            db.execute(select(Yard).order_by(Yard.code)).scalars().all(),
            lambda row: row.code,
        ),
        "areas": as_options(
            db.execute(select(Area).order_by(Area.code)).scalars().all(),
            lambda row: row.code,
        ),
        "waste_codes": as_options(
            db.execute(select(WasteCode).order_by(WasteCode.code)).scalars().all(),
            lambda row: row.code,
        ),
        "waste_producers": as_options(
            db.execute(select(WasteProducer).order_by(WasteProducer.name)).scalars().all(),
            lambda row: row.name,
        ),
        "licences": as_options(
            db.execute(select(Licence).order_by(Licence.code)).scalars().all(),
            lambda row: row.code,
        ),
        "units": as_options(
            db.execute(select(Unit).order_by(Unit.code)).scalars().all(),
            lambda row: row.code,
        ),
        "void_reasons": as_options(
            db.execute(select(VoidReason).order_by(VoidReason.code)).scalars().all(),
            lambda row: row.description or row.code,
        ),
    }


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
        "waste_codes",
        "waste_producers",
        "licences",
        "units",
        "void_reasons",
    ]


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def tickets_edit(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    is_admin = True
    invoice = db.get(Invoice, ticket.invoice_id) if ticket.invoice_id else None
    return templates.TemplateResponse(
        "tickets/edit.html",
        {
            "request": request,
            "errors": [],
            "saved": request.query_params.get("saved") == "1",
            "completed": request.query_params.get("completed") == "1",
            "ticket": ticket,
            "invoice": invoice,
            "is_admin": is_admin,
            "weight_warning": _net_negative(ticket),
            "direction_warning": _direction_transaction_warning(
                ticket.direction, ticket.transaction_type
            ),
            "form": _ticket_to_form(ticket),
            "options": _load_ticket_options(db),
            "enums": _ticket_enums(),
        },
    )


@router.post("/tickets/{ticket_id}", response_class=HTMLResponse)
async def tickets_update(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return templates.TemplateResponse(
            "tickets/not_found.html",
            {"request": request, "ticket_id": ticket_id},
            status_code=404,
        )

    is_admin = True
    form = await request.form()
    action = str(form.get("action", "save"))
    direction_warning = _direction_transaction_warning(
        ticket.direction, ticket.transaction_type
    )

    if action == "save" and ticket.status == TicketStatusEnum.VOID:
        return templates.TemplateResponse(
            "tickets/edit.html",
            {
                "request": request,
                "errors": ["Ticket is locked."],
                "ticket": ticket,
                "is_admin": is_admin,
                "weight_warning": _net_negative(ticket),
                "direction_warning": direction_warning,
                "form": _ticket_to_form(ticket),
                "options": _load_ticket_options(db),
                "enums": _ticket_enums(),
            },
            status_code=400,
        )

    if action == "complete":
        payload = _parse_ticket_form(
            form, current_status=ticket.status.value if ticket.status else None
        )
        gross_kg = payload["gross_kg"]
        tare_kg = payload["tare_kg"]
        qty = payload["qty"]
        unit_price = payload["unit_price"]
        product_id = payload["product_id"]
        has_weights = gross_kg is not None and tare_kg is not None
        has_pricing = (
            qty is not None
            and float(qty) > 0
            and unit_price is not None
            and unit_price >= 0
        )
        weight_warning = _net_negative_values(gross_kg, tare_kg)
        direction_warning = _direction_transaction_warning(
            payload["direction"], payload["transaction_type"]
        )
        if ticket.status in (TicketStatusEnum.COMPLETE, TicketStatusEnum.VOID):
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": ["Ticket is locked."],
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": weight_warning,
                    "direction_warning": direction_warning,
                    "form": payload["form"],
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
        if not (has_weights or has_pricing):
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": ["Enter Gross+Tare OR Quantity+Unit Price."],
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": weight_warning,
                    "direction_warning": direction_warning,
                    "form": payload["form"],
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
        if has_pricing and product_id is None:
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": ["Product is required when using pricing."],
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": weight_warning,
                    "direction_warning": direction_warning,
                    "form": payload["form"],
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
        if has_weights and weight_warning:
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": ["Gross is lower than Tare - use Swap Weights."],
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": False,
                    "direction_warning": direction_warning,
                    "form": payload["form"],
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
        _apply_ticket_updates(ticket, payload)
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
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": errors,
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": _net_negative(ticket),
                    "direction_warning": direction_warning,
                    "form": _ticket_to_form(ticket),
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
        if ticket.status == TicketStatusEnum.VOID:
            return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)
        ticket.status = TicketStatusEnum.VOID.value
        db.add(
            TicketVoid(
                ticket_id=ticket.id,
                reason_id=reason_id,
                note=note,
                voided_at=datetime.utcnow(),
                voided_by="admin",
            )
        )
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)

    payload = _parse_ticket_form(
        form, current_status=ticket.status.value if ticket.status else None
    )
    direction_warning = _direction_transaction_warning(
        payload["direction"], payload["transaction_type"]
    )
    if ticket.status == TicketStatusEnum.COMPLETE:
        if _completed_ticket_locked_changes(ticket, payload):
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": ["Ticket is locked."],
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "weight_warning": _net_negative(ticket),
                    "direction_warning": direction_warning,
                    "form": payload["form"],
                    "options": _load_ticket_options(db),
                    "enums": _ticket_enums(),
                },
                status_code=400,
            )
    _apply_ticket_defaults(db, payload)
    errors = payload["errors"]
    if errors:
        return templates.TemplateResponse(
            "tickets/edit.html",
            {
                "request": request,
                "errors": errors,
                "ticket": ticket,
                "is_admin": is_admin,
                "weight_warning": _net_negative(ticket),
                "direction_warning": direction_warning,
                "form": payload["form"],
                "options": _load_ticket_options(db),
                "enums": _ticket_enums(),
            },
            status_code=400,
        )

    if ticket.status == "COMPLETE" and not is_admin:
        payload["gross_kg"] = ticket.gross_kg
        payload["tare_kg"] = ticket.tare_kg
        payload["net_kg"] = ticket.net_kg

    _apply_ticket_updates(ticket, payload)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}?saved=1", status_code=303)


@router.post("/tickets/{ticket_id}/weights/gross", response_class=HTMLResponse)
async def tickets_capture_gross(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if ticket.status in (TicketStatusEnum.COMPLETE.value, TicketStatusEnum.VOID.value):
        return _render_weights_partial(
            request, ticket, errors=["Ticket is locked."], status_code=403
        )

    form = await request.form()
    gross_value = _parse_float(_form_value(form, "weight_value"))
    if gross_value is None:
        return _render_weights_partial(
            request, ticket, errors=["Gross weight is required."], status_code=400
        )

    ticket.gross_kg = gross_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = datetime.utcnow()
    db.commit()
    return _render_weights_partial(request, ticket, errors=[])




@router.post("/tickets/{ticket_id}/weights/tare", response_class=HTMLResponse)
async def tickets_capture_tare(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    if ticket.status in (TicketStatusEnum.COMPLETE.value, TicketStatusEnum.VOID.value):
        return _render_weights_partial(
            request, ticket, errors=["Ticket is locked."], status_code=403
        )

    form = await request.form()
    tare_value = _parse_float(_form_value(form, "weight_value"))
    if tare_value is None:
        return _render_weights_partial(
            request, ticket, errors=["Tare weight is required."], status_code=400
        )

    ticket.tare_kg = tare_value
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = datetime.utcnow()
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
    readout_value = _parse_float(readout_raw)
    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_value = _parse_float(gross_raw)
    tare_value = _parse_float(tare_raw)

    errors: list[str] = []
    read_confirm = False

    if readout_value is None:
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

    return templates.TemplateResponse(
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
    readout_value = _parse_float(readout_raw)
    gross_raw = _form_value(form, "gross_kg")
    tare_raw = _form_value(form, "tare_kg")
    gross_value = _parse_float(gross_raw)
    tare_value = _parse_float(tare_raw)

    errors: list[str] = []

    if readout_value is None:
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

    return templates.TemplateResponse(
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

    return templates.TemplateResponse(
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

    ticket.gross_kg, ticket.tare_kg = ticket.tare_kg, ticket.gross_kg
    ticket.net_kg = (
        ticket.gross_kg - ticket.tare_kg
        if ticket.gross_kg is not None and ticket.tare_kg is not None
        else None
    )
    ticket.updated_at = datetime.utcnow()
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
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
    ticket.direction = payload["direction"]
    ticket.transaction_type = payload["transaction_type"]
    ticket.status = payload["status"]
    ticket.customer_id = payload["customer_id"]
    ticket.vehicle_id = payload["vehicle_id"]
    ticket.product_id = payload["product_id"]
    ticket.haulier_id = payload["haulier_id"]
    ticket.driver_id = payload["driver_id"]
    ticket.container_id = payload["container_id"]
    ticket.destination_id = payload["destination_id"]
    ticket.yard_id = payload["yard_id"]
    ticket.area_id = payload["area_id"]
    ticket.waste_code_id = payload["waste_code_id"]
    ticket.waste_producer_id = payload["waste_producer_id"]
    ticket.licence_id = payload["licence_id"]
    ticket.gross_kg = payload["gross_kg"]
    ticket.tare_kg = payload["tare_kg"]
    ticket.net_kg = payload["net_kg"]
    ticket.qty = payload["qty"]
    ticket.unit_id = payload["unit_id"]
    ticket.unit_price = payload["unit_price"]
    ticket.total = payload["total"]
    ticket.dont_invoice = payload["dont_invoice"]
    ticket.updated_at = datetime.utcnow()


def _parse_ticket_form(form, current_status: str | None = None) -> dict:
    errors: list[str] = []

    datetime_raw = _form_value(form, "datetime")
    direction = _form_value(form, "direction")
    transaction_type = _form_value(form, "transaction_type")
    status = current_status or TicketStatusEnum.OPEN.value
    customer_id = _parse_int(_form_value(form, "customer_id"))
    vehicle_id = _parse_int(_form_value(form, "vehicle_id"))
    product_id = _parse_int(_form_value(form, "product_id"))

    if not datetime_raw:
        errors.append("Date/time is required.")
    if not direction:
        errors.append("Direction is required.")
    if not transaction_type:
        errors.append("Transaction type is required.")
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

    gross_kg = _parse_float(_form_value(form, "gross_kg"))
    tare_kg = _parse_float(_form_value(form, "tare_kg"))
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
        "direction": direction,
        "transaction_type": transaction_type,
        "status": status,
        "customer_id": _form_value(form, "customer_id"),
        "vehicle_id": _form_value(form, "vehicle_id"),
        "product_id": _form_value(form, "product_id"),
        "haulier_id": _form_value(form, "haulier_id"),
        "driver_id": _form_value(form, "driver_id"),
        "container_id": _form_value(form, "container_id"),
        "destination_id": _form_value(form, "destination_id"),
        "yard_id": _form_value(form, "yard_id"),
        "area_id": _form_value(form, "area_id"),
        "waste_code_id": _form_value(form, "waste_code_id"),
        "waste_producer_id": _form_value(form, "waste_producer_id"),
        "licence_id": _form_value(form, "licence_id"),
        "gross_kg": _form_value(form, "gross_kg"),
        "tare_kg": _form_value(form, "tare_kg"),
        "net_kg": f"{net_kg:.3f}" if net_kg is not None else "",
        "qty": _form_value(form, "qty"),
        "unit_id": _form_value(form, "unit_id"),
        "unit_price": _form_value(form, "unit_price"),
        "total": f"{total:.2f}" if total is not None else "",
        "dont_invoice": "on" if dont_invoice else "",
    }

    return {
        "errors": errors,
        "form": form_data,
        "ticket_datetime": ticket_datetime or datetime.utcnow(),
        "direction": direction,
        "transaction_type": transaction_type,
        "status": status,
        "customer_id": customer_id,
        "vehicle_id": vehicle_id,
        "product_id": product_id,
        "haulier_id": _parse_int(_form_value(form, "haulier_id")),
        "driver_id": _parse_int(_form_value(form, "driver_id")),
        "container_id": _parse_int(_form_value(form, "container_id")),
        "destination_id": _parse_int(_form_value(form, "destination_id")),
        "yard_id": _parse_int(_form_value(form, "yard_id")),
        "area_id": _parse_int(_form_value(form, "area_id")),
        "waste_code_id": _parse_int(_form_value(form, "waste_code_id")),
        "waste_producer_id": _parse_int(_form_value(form, "waste_producer_id")),
        "licence_id": _parse_int(_form_value(form, "licence_id")),
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
        "product_id": str(ticket.product_id or ""),
        "haulier_id": str(ticket.haulier_id or ""),
        "driver_id": str(ticket.driver_id or ""),
        "container_id": str(ticket.container_id or ""),
        "destination_id": str(ticket.destination_id or ""),
        "yard_id": str(ticket.yard_id or ""),
        "area_id": str(ticket.area_id or ""),
        "waste_code_id": str(ticket.waste_code_id or ""),
        "waste_producer_id": str(ticket.waste_producer_id or ""),
        "licence_id": str(ticket.licence_id or ""),
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


def _same_decimal(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


def _completed_ticket_locked_changes(ticket: Ticket, payload: dict) -> bool:
    if payload["customer_id"] != ticket.customer_id:
        return True
    if payload["product_id"] != ticket.product_id:
        return True
    if payload["vehicle_id"] != ticket.vehicle_id:
        return True
    if payload["direction"] != ticket.direction:
        return True
    if payload["transaction_type"] != ticket.transaction_type:
        return True
    if not _same_decimal(payload["gross_kg"], ticket.gross_kg):
        return True
    if not _same_decimal(payload["tare_kg"], ticket.tare_kg):
        return True
    if not _same_decimal(payload["qty"], ticket.qty):
        return True
    if payload["unit_id"] != ticket.unit_id:
        return True
    if not _same_decimal(payload["unit_price"], ticket.unit_price):
        return True
    return False


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


def _render_weights_partial(
    request: Request, ticket: Ticket, errors: list[str], status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
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
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

