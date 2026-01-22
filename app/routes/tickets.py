from datetime import date, datetime, time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Area,
    Container,
    Customer,
    Destination,
    Driver,
    Haulier,
    Licence,
    Product,
    Ticket,
    TicketVoid,
    Unit,
    Vehicle,
    VoidReason,
    WasteCode,
    WasteProducer,
    Yard,
)
from ..services.weight_source import get_indicator_source

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/tickets", response_class=HTMLResponse)
def tickets_list(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    direction: str | None = None,
    transaction_type: str | None = None,
    customer: str | None = None,
    vehicle: str | None = None,
    ticket_no: str | None = None,
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
    if customer:
        filters.append(
            or_(
                Customer.name.ilike(f"%{customer}%"),
                Customer.account_code.ilike(f"%{customer}%"),
            )
        )
    if vehicle:
        filters.append(Vehicle.registration.ilike(f"%{vehicle}%"))

    base_stmt = (
        select(Ticket, Customer, Vehicle, Product)
        .outerjoin(Customer, Ticket.customer_id == Customer.id)
        .outerjoin(Vehicle, Ticket.vehicle_id == Vehicle.id)
        .outerjoin(Product, Ticket.product_id == Product.id)
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

    rows = (
        db.execute(
            base_stmt.order_by(Ticket.datetime.desc())
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
                "customer": customer or "",
                "vehicle": vehicle or "",
                "ticket_no": ticket_no or "",
            },
        },
    )


@router.get("/tickets/new", response_class=HTMLResponse)
def tickets_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "tickets/new.html",
        {
            "request": request,
            "errors": [],
            "form": {
                "datetime": datetime.now().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
                "direction": "",
                "transaction_type": "",
                "status": "OPEN",
                "customer_id": "",
                "vehicle_id": "",
                "product_id": "",
                "haulier_id": "",
                "driver_id": "",
                "container_id": "",
                "destination_id": "",
                "yard_id": "",
                "area_id": "",
                "waste_code_id": "",
                "waste_producer_id": "",
                "licence_id": "",
                "gross_kg": "",
                "tare_kg": "",
                "net_kg": "",
                "qty": "",
                "unit_id": "",
                "unit_price": "",
                "total": "",
                "dont_invoice": "",
            },
            "options": _load_ticket_options(db),
        },
    )


@router.post("/tickets/new", response_class=HTMLResponse)
async def tickets_create(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_ticket_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "tickets/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_ticket_options(db),
            },
            status_code=400,
        )

    ticket = Ticket(
        ticket_no=_generate_ticket_no(db),
        datetime=payload["ticket_datetime"],
        direction=payload["direction"],
        transaction_type=payload["transaction_type"],
        status=payload["status"],
        customer_id=payload["customer_id"],
        vehicle_id=payload["vehicle_id"],
        product_id=payload["product_id"],
        haulier_id=payload["haulier_id"],
        driver_id=payload["driver_id"],
        container_id=payload["container_id"],
        destination_id=payload["destination_id"],
        yard_id=payload["yard_id"],
        area_id=payload["area_id"],
        waste_code_id=payload["waste_code_id"],
        waste_producer_id=payload["waste_producer_id"],
        licence_id=payload["licence_id"],
        gross_kg=payload["gross_kg"],
        tare_kg=payload["tare_kg"],
        net_kg=payload["net_kg"],
        qty=payload["qty"],
        unit_id=payload["unit_id"],
        unit_price=payload["unit_price"],
        total=payload["total"],
        dont_invoice=payload["dont_invoice"],
    )
    db.add(ticket)
    db.commit()

    return RedirectResponse(url="/tickets", status_code=303)


def _generate_ticket_no(db: Session) -> str:
    while True:
        stamp = datetime.utcnow().strftime("T%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:4].upper()
        ticket_no = f"{stamp}-{suffix}"
        exists = db.execute(
            select(Ticket.id).where(Ticket.ticket_no == ticket_no)
        ).first()
        if not exists:
            return ticket_no


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
            lambda row: row.code,
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
    return templates.TemplateResponse(
        "tickets/edit.html",
        {
            "request": request,
            "errors": [],
            "ticket": ticket,
            "is_admin": is_admin,
            "form": _ticket_to_form(ticket),
            "options": _load_ticket_options(db),
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

    if action == "complete":
        ticket.status = "COMPLETE"
        db.commit()
        return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)

    if action == "void":
        reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
        note = str(form.get("void_note", "")).strip()
        errors = []
        if not reason_id:
            errors.append("Void reason is required.")
        if not note:
            errors.append("Void note is required.")
        if errors:
            return templates.TemplateResponse(
                "tickets/edit.html",
                {
                    "request": request,
                    "errors": errors,
                    "ticket": ticket,
                    "is_admin": is_admin,
                    "form": _ticket_to_form(ticket),
                    "options": _load_ticket_options(db),
                },
                status_code=400,
            )
        ticket.status = "VOID"
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

    payload = _parse_ticket_form(form)
    errors = payload["errors"]
    if errors:
        return templates.TemplateResponse(
            "tickets/edit.html",
            {
                "request": request,
                "errors": errors,
                "ticket": ticket,
                "is_admin": is_admin,
                "form": payload["form"],
                "options": _load_ticket_options(db),
            },
            status_code=400,
        )

    if ticket.status == "COMPLETE" and not is_admin:
        payload["gross_kg"] = ticket.gross_kg
        payload["tare_kg"] = ticket.tare_kg
        payload["net_kg"] = ticket.net_kg

    _apply_ticket_updates(ticket, payload)
    db.commit()
    return RedirectResponse(url=f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/weights/gross", response_class=HTMLResponse)
async def tickets_capture_gross(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)

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


@router.get("/tickets/{ticket_id}/weights/gross/form", response_class=HTMLResponse)
def tickets_capture_gross_form(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    indicator = get_indicator_source()
    if indicator.is_connected():
        weight = indicator.get_weight_kg()
        if weight is not None:
            ticket.gross_kg = weight
            ticket.net_kg = (
                ticket.gross_kg - ticket.tare_kg
                if ticket.gross_kg is not None and ticket.tare_kg is not None
                else None
            )
            ticket.updated_at = datetime.utcnow()
            db.commit()
            return _render_weights_partial(request, ticket, errors=[])
    return templates.TemplateResponse(
        "tickets/_weight_form.html",
        {
            "request": request,
            "ticket": ticket,
            "field": "gross",
            "placeholder": "Gross kg",
        },
    )


@router.post("/tickets/{ticket_id}/weights/tare", response_class=HTMLResponse)
async def tickets_capture_tare(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)

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


@router.get("/tickets/{ticket_id}/weights/tare/form", response_class=HTMLResponse)
def tickets_capture_tare_form(
    ticket_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    ticket = db.get(Ticket, ticket_id)
    if not ticket:
        return HTMLResponse("Ticket not found.", status_code=404)
    indicator = get_indicator_source()
    if indicator.is_connected():
        weight = indicator.get_weight_kg()
        if weight is not None:
            ticket.tare_kg = weight
            ticket.net_kg = (
                ticket.gross_kg - ticket.tare_kg
                if ticket.gross_kg is not None and ticket.tare_kg is not None
                else None
            )
            ticket.updated_at = datetime.utcnow()
            db.commit()
            return _render_weights_partial(request, ticket, errors=[])
    return templates.TemplateResponse(
        "tickets/_weight_form.html",
        {
            "request": request,
            "ticket": ticket,
            "field": "tare",
            "placeholder": "Tare kg",
        },
    )


@router.post("/tickets/{ticket_id}/weights/swap", response_class=HTMLResponse)
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
    return _render_weights_partial(request, ticket, errors=[])


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


def _parse_ticket_form(form) -> dict:
    errors: list[str] = []

    datetime_raw = _form_value(form, "datetime")
    direction = _form_value(form, "direction")
    transaction_type = _form_value(form, "transaction_type")
    status = _form_value(form, "status") or "OPEN"
    customer_id = _parse_int(_form_value(form, "customer_id"))
    vehicle_id = _parse_int(_form_value(form, "vehicle_id"))
    product_id = _parse_int(_form_value(form, "product_id"))

    if not datetime_raw:
        errors.append("Date/time is required.")
    if not direction:
        errors.append("Direction is required.")
    if not transaction_type:
        errors.append("Transaction type is required.")
    if not customer_id:
        errors.append("Customer is required.")
    if not vehicle_id:
        errors.append("Vehicle is required.")
    if not product_id:
        errors.append("Product is required.")

    ticket_datetime: datetime | None = None
    if datetime_raw:
        try:
            ticket_datetime = datetime.fromisoformat(datetime_raw)
        except ValueError:
            errors.append("Date/time must be valid.")

    gross_kg = _parse_float(_form_value(form, "gross_kg"))
    tare_kg = _parse_float(_form_value(form, "tare_kg"))
    qty = _parse_float(_form_value(form, "qty"))
    unit_price = _parse_float(_form_value(form, "unit_price"))
    net_kg = (
        gross_kg - tare_kg if gross_kg is not None and tare_kg is not None else None
    )
    total = qty * unit_price if qty is not None and unit_price is not None else None
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
    }


def _ticket_to_form(ticket: Ticket) -> dict:
    return {
        "datetime": ticket.datetime.isoformat(timespec="minutes")
        if ticket.datetime
        else "",
        "direction": ticket.direction or "",
        "transaction_type": ticket.transaction_type or "",
        "status": ticket.status or "",
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
        "gross_kg": f"{ticket.gross_kg}" if ticket.gross_kg is not None else "",
        "tare_kg": f"{ticket.tare_kg}" if ticket.tare_kg is not None else "",
        "net_kg": f"{ticket.net_kg}" if ticket.net_kg is not None else "",
        "qty": f"{ticket.qty}" if ticket.qty is not None else "",
        "unit_id": str(ticket.unit_id or ""),
        "unit_price": f"{ticket.unit_price}" if ticket.unit_price is not None else "",
        "total": f"{ticket.total}" if ticket.total is not None else "",
        "dont_invoice": "on" if ticket.dont_invoice else "",
    }


def _form_value(form, key: str) -> str:
    return str(form.get(key, "")).strip()


def _render_weights_partial(
    request: Request, ticket: Ticket, errors: list[str], status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        "tickets/_weights.html",
        {
            "request": request,
            "ticket": ticket,
            "errors": errors,
            "is_admin": True,
            "form": _ticket_to_form(ticket),
        },
        status_code=status_code,
    )


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
