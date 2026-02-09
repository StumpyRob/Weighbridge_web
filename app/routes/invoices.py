from datetime import date, datetime, time, timedelta
import logging
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session, joinedload

from ..db import get_db
from ..models.base import utcnow
from ..models import (
    Customer,
    Invoice,
    InvoiceLine,
    InvoiceVoid,
    PaymentMethod,
    Product,
    TaxRate,
    Ticket,
    VoidReason,
)
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


LOCKED_INVOICE_STATUSES = {"VOID"}


def _is_other_void_reason(reason: VoidReason | None) -> bool:
    if not reason:
        return False
    code = (reason.code or "").strip().lower()
    description = (reason.description or "").strip().lower()
    return code == "other" or description == "other"


@router.get("/invoices", response_class=HTMLResponse)
def invoices_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = (
        select(Invoice, Customer)
        .join(Customer, Invoice.customer_id == Customer.id)
        .order_by(Invoice.invoice_date.desc())
    )
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Invoice.invoice_no.ilike(like), Customer.name.ilike(like))
        )
    rows = db.execute(query).all()
    return templates.TemplateResponse(request, 
        "invoices/list.html", {"request": request, "rows": rows, "q": q or ""}
    )


@router.get("/invoices/generate", response_class=HTMLResponse)
def invoices_generate_form(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    return templates.TemplateResponse(request, 
        "invoices/generate.html",
        {
            "request": request,
            "errors": [],
            "customers": customers,
            "form": {"customer_id": "", "date_from": "", "date_to": ""},
        },
    )


@router.post("/invoices/generate", response_class=HTMLResponse)
async def invoices_generate(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    customer_id = _parse_int(str(form.get("customer_id", "")).strip())
    date_from_raw = str(form.get("date_from", "")).strip()
    date_to_raw = str(form.get("date_to", "")).strip()

    errors: list[str] = []
    if not customer_id:
        errors.append("Customer is required.")

    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)
    if date_from_raw and not date_from:
        errors.append("Start date must be valid.")
    if date_to_raw and not date_to:
        errors.append("End date must be valid.")
    if date_from and date_to and date_to < date_from:
        errors.append("Date range invalid.")

    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    if errors:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": errors,
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        tickets = _fetch_ticket_candidates(db, customer_id, date_from, date_to)
        invoiceable_tickets = _fetch_invoiceable_ticket_candidates(
            db, customer_id, date_from, date_to
        )
        stop_blockers = _invoice_stop_blockers(db, customer_id, invoiceable_tickets)
        if stop_blockers:
            return templates.TemplateResponse(
                request,
                "invoices/generate.html",
                {
                    "request": request,
                    "errors": [_invoice_on_stop_error(stop_blockers)],
                    "customers": customers,
                    "form": {
                        "customer_id": str(customer_id or ""),
                        "date_from": date_from_raw,
                        "date_to": date_to_raw,
                    },
                },
            )
        included, excluded = _classify_tickets(tickets)
        included_total = sum(
            (_money(ticket.total) for ticket in included), Decimal("0.00")
        )
    except Exception:
        logger.exception("Invoice preview failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the preview."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    if not tickets:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["No tickets found."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    return templates.TemplateResponse(request, 
        "invoices/generate.html",
        {
            "request": request,
            "errors": [],
            "customers": customers,
            "form": {
                "customer_id": str(customer_id or ""),
                "date_from": date_from_raw,
                "date_to": date_to_raw,
            },
            "preview": {
                "included": included,
                "excluded": excluded,
                "included_total": _money(included_total),
            },
        },
    )


@router.post("/invoices/generate/confirm", response_class=HTMLResponse)
async def invoices_generate_confirm(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    customer_id = _parse_int(str(form.get("customer_id", "")).strip())
    date_from_raw = str(form.get("date_from", "")).strip()
    date_to_raw = str(form.get("date_to", "")).strip()
    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)

    errors: list[str] = []
    if not customer_id:
        errors.append("Customer is required.")
    if date_from_raw and not date_from:
        errors.append("Start date must be valid.")
    if date_to_raw and not date_to:
        errors.append("End date must be valid.")
    if date_from and date_to and date_to < date_from:
        errors.append("Date range invalid.")

    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    if errors:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": errors,
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        candidate_tickets = _fetch_invoiceable_ticket_candidates(
            db, customer_id, date_from, date_to
        )
    except Exception:
        logger.exception("Invoice candidate query failed")
        return templates.TemplateResponse(
            request,
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )
    stop_blockers = _invoice_stop_blockers(db, customer_id, candidate_tickets)
    if stop_blockers:
        return templates.TemplateResponse(
            request,
            "invoices/generate.html",
            {
                "request": request,
                "errors": [_invoice_on_stop_error(stop_blockers)],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    ticket_filters = _invoiceable_ticket_filters(customer_id, date_from, date_to)

    try:
        ticket_rows = db.execute(
            select(Ticket, Product, TaxRate)
            .join(Product, Ticket.product_id == Product.id)
            .outerjoin(TaxRate, Product.tax_rate_id == TaxRate.id)
            .where(and_(*ticket_filters))
            .order_by(Ticket.datetime.asc())
        ).all()
    except Exception:
        logger.exception("Invoice confirm query failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    if not ticket_rows:
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["No invoiceable tickets found."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    try:
        invoice = Invoice(
            invoice_no=_generate_invoice_no(db),
            customer_id=customer_id,
            invoice_date=date.today(),
            status="DRAFT",
            net_total=Decimal("0.00"),
            vat_total=Decimal("0.00"),
            gross_total=Decimal("0.00"),
        )
        db.add(invoice)
        db.flush()

        line_totals: list[tuple[Decimal, Decimal]] = []

        for ticket, product, tax_rate in ticket_rows:
            net = _money(ticket.total)
            raw_rate = _decimal(tax_rate.rate_percent) if tax_rate else Decimal("0")
            rate = raw_rate / Decimal("100") if raw_rate > 1 else raw_rate
            vat = _money(net * rate)
            gross = net + vat

            line = InvoiceLine(
                invoice_id=invoice.id,
                ticket_id=ticket.id,
                description=f"Ticket {ticket.ticket_no} - {product.description}",
                quantity=float(ticket.qty or 0),
                unit_price=_money(ticket.unit_price),
                net=net,
                vat=vat,
                gross=gross,
            )
            db.add(line)
            ticket.invoice_id = invoice.id
            line_totals.append((net, vat))

        net_total = _money(sum((net for net, _ in line_totals), Decimal("0.00")))
        vat_total = _money(sum((vat for _, vat in line_totals), Decimal("0.00")))
        invoice.net_total = net_total
        invoice.vat_total = vat_total
        invoice.gross_total = _money(net_total + vat_total)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Invoice creation failed")
        return templates.TemplateResponse(request, 
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["Something went wrong generating the invoice."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
        )

    return RedirectResponse(url=f"/invoices/{invoice.id}?created=1", status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoices_detail(
    invoice_id: int,
    request: Request,
    created: int | None = Query(None),
    paid: int | None = Query(None),
    voided: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "invoices/detail.html",
        _invoice_detail_context(
            request,
            db,
            invoice,
            errors=[],
            created=created == 1,
            paid=paid == 1,
            voided=voided == 1,
        ),
    )


@router.post("/invoices/{invoice_id}/paid", response_class=HTMLResponse)
async def invoices_mark_paid(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    if invoice.status in LOCKED_INVOICE_STATUSES:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=[f"Invoice is {invoice.status} and cannot be modified."],
            ),
            status_code=400,
        )
    if invoice.status == "PAID":
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Already paid."],
            ),
            status_code=400,
        )

    form = await request.form()
    payment_method_id = _parse_int(str(form.get("payment_method_id", "")).strip())
    paid_at_raw = str(form.get("paid_at", "")).strip()
    errors: list[str] = []
    if not payment_method_id:
        errors.append("Payment method is required.")
    if not paid_at_raw:
        errors.append("Paid date is required.")
    if errors:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(request, db, invoice, errors=errors),
            status_code=400,
        )

    payment_method = db.get(PaymentMethod, payment_method_id)
    if not payment_method or not payment_method.is_active:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Payment method is invalid."],
            ),
            status_code=400,
        )

    paid_at = _parse_datetime(paid_at_raw)

    if not paid_at:
        return templates.TemplateResponse(request, 
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Paid date must be valid."]
            ),
            status_code=400,
        )

    invoice.status = "PAID"
    invoice.payment_method_id = payment_method.id
    invoice.paid_at = paid_at
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}?paid=1", status_code=303)


@router.post("/invoices/{invoice_id}/void", response_class=HTMLResponse)
async def invoices_void(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(request, 
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    if invoice.status in LOCKED_INVOICE_STATUSES:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=[f"Invoice is {invoice.status} and cannot be modified."],
            ),
            status_code=400,
        )
    if invoice.status == "PAID":
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Invoice is PAID and cannot be modified."],
            ),
            status_code=400,
        )

    form = await request.form()
    reason_id = _parse_int(str(form.get("void_reason_id", "")).strip())
    note = str(form.get("void_note", "")).strip()
    reason = db.get(VoidReason, reason_id) if reason_id else None

    if not reason_id:
        return templates.TemplateResponse(request, 
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Void reason is required."]
            ),
            status_code=400,
        )
    if not reason or not reason.is_active:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request, db, invoice, errors=["Void reason is invalid."]
            ),
            status_code=400,
        )
    if _is_other_void_reason(reason) and not note:
        return templates.TemplateResponse(
            request,
            "invoices/detail.html",
            _invoice_detail_context(
                request,
                db,
                invoice,
                errors=["Void note is required when reason is Other."],
            ),
            status_code=400,
        )

    invoice.status = "VOID"
    db.add(
        InvoiceVoid(
            invoice_id=invoice.id,
            reason_id=reason_id,
            note=note or "No note provided.",
            voided_at=utcnow(),
            voided_by="admin",
        )
    )
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}?voided=1", status_code=303)


def _generate_invoice_no(db: Session) -> str:
    year = utcnow().year
    db.execute(
        text(
            "INSERT OR IGNORE INTO invoice_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": utcnow()},
    )
    db.execute(
        text(
            "UPDATE invoice_sequences "
            "SET last_number = last_number + 1, updated_at = :updated_at "
            "WHERE year = :year"
        ),
        {"year": year, "updated_at": utcnow()},
    )
    next_number = db.execute(
        text("SELECT last_number FROM invoice_sequences WHERE year = :year"),
        {"year": year},
    ).scalar_one()

    return f"INV-{str(year)[2:]}-{next_number:05d}"


def _parse_date(value: str) -> date | None:
    if not value:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _active_payment_methods(db: Session) -> list[PaymentMethod]:
    return (
        db.execute(
            select(PaymentMethod)
            .where(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.code)
        )
        .scalars()
        .all()
    )


def _active_void_reasons(db: Session) -> list[VoidReason]:
    return (
        db.execute(
            select(VoidReason)
            .where(VoidReason.is_active.is_(True))
            .order_by(VoidReason.code)
        )
        .scalars()
        .all()
    )


def _invoice_detail_context(
    request: Request,
    db: Session,
    invoice: Invoice,
    *,
    errors: list[str] | None = None,
    created: bool = False,
    paid: bool = False,
    voided: bool = False,
) -> dict:
    customer = db.get(Customer, invoice.customer_id)
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.id)
    ).scalars().all()
    tickets = db.execute(
        select(Ticket).where(Ticket.invoice_id == invoice.id).order_by(Ticket.datetime)
    ).scalars().all()
    return {
        "request": request,
        "invoice": invoice,
        "customer": customer,
        "lines": lines,
        "tickets": tickets,
        "payment_methods": _active_payment_methods(db),
        "void_reasons": _active_void_reasons(db),
        "errors": errors or [],
        "created": created,
        "paid": paid,
        "voided": voided,
    }


def _invoice_stop_blockers(
    db: Session, customer_id: int, tickets: list[Ticket]
) -> list[str]:
    blockers: list[str] = []
    customer = db.get(Customer, customer_id)
    if customer and customer.on_stop:
        blockers.append("Customer")
    if any(bool(getattr(ticket.haulier, "on_stop", False)) for ticket in tickets):
        blockers.append("Haulier")
    return blockers


def _invoice_on_stop_error(blockers: list[str]) -> str:
    if not blockers:
        return ""
    if len(blockers) == 1:
        subject = blockers[0]
        verb = "is"
    else:
        subject = "Customer and Haulier"
        verb = "are"
    return (
        f"Cannot generate invoice: {subject} {verb} ON STOP. "
        "You may record the ticket, but it cannot be completed/invoiced until stop is removed."
    )


def _fetch_ticket_candidates(
    db: Session, customer_id: int, date_from: date | None, date_to: date | None
) -> list[Ticket]:
    filters = [Ticket.customer_id == customer_id]
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    return (
        db.execute(
            select(Ticket)
            .options(joinedload(Ticket.haulier))
            .where(and_(*filters))
            .order_by(Ticket.datetime.asc())
        )
        .scalars()
        .all()
    )


def _invoiceable_ticket_filters(
    customer_id: int, date_from: date | None, date_to: date | None
) -> list:
    filters = [
        Ticket.customer_id == customer_id,
        Ticket.status == "COMPLETE",
        Ticket.status != "VOID",
        Ticket.dont_invoice.is_(False),
        Ticket.invoice_id.is_(None),
        Ticket.qty.is_not(None),
        Ticket.qty > 0,
        Ticket.unit_price.is_not(None),
        Ticket.unit_price >= 0,
        Ticket.total.is_not(None),
        Ticket.total > 0,
    ]
    # Date filters are interpreted in server-local time (UTC by default).
    if date_from:
        filters.append(Ticket.datetime >= datetime.combine(date_from, time.min))
    if date_to:
        end_exclusive = datetime.combine(date_to + timedelta(days=1), time.min)
        filters.append(Ticket.datetime < end_exclusive)
    return filters


def _fetch_invoiceable_ticket_candidates(
    db: Session, customer_id: int, date_from: date | None, date_to: date | None
) -> list[Ticket]:
    filters = _invoiceable_ticket_filters(customer_id, date_from, date_to)
    return (
        db.execute(
            select(Ticket)
            .options(joinedload(Ticket.haulier))
            .where(and_(*filters))
            .order_by(Ticket.datetime.asc())
        )
        .scalars()
        .all()
    )


def _classify_tickets(
    tickets: list[Ticket],
) -> tuple[list[Ticket], list[tuple[Ticket, str]]]:
    included: list[Ticket] = []
    excluded: list[tuple[Ticket, str]] = []

    for ticket in tickets:
        if ticket.status == "VOID":
            excluded.append((ticket, "Voided"))
            continue
        if ticket.status != "COMPLETE":
            excluded.append((ticket, "Not complete"))
            continue
        if ticket.dont_invoice:
            excluded.append((ticket, "Don't invoice"))
            continue
        if ticket.invoice_id is not None:
            excluded.append((ticket, "Already invoiced"))
            continue
        if ticket.qty is None or float(ticket.qty) <= 0:
            excluded.append((ticket, "Missing quantity/price"))
            continue
        if ticket.unit_price is None or ticket.unit_price < 0:
            excluded.append((ticket, "Missing quantity/price"))
            continue
        if ticket.total is None or ticket.total <= 0:
            excluded.append((ticket, "Zero total"))
            continue

        included.append(ticket)

    return included, excluded
