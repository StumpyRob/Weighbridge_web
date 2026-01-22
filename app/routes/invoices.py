from datetime import date, datetime, time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Customer, Invoice, InvoiceLine, PaymentMethod, Product, TaxRate, Ticket

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    return templates.TemplateResponse(
        "invoices/list.html", {"request": request, "rows": rows, "q": q or ""}
    )


@router.get("/invoices/generate", response_class=HTMLResponse)
def invoices_generate_form(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
    return templates.TemplateResponse(
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

    if errors:
        customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
        return templates.TemplateResponse(
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
            status_code=400,
        )

    ticket_filters = [
        Ticket.customer_id == customer_id,
        Ticket.status == "COMPLETE",
        Ticket.dont_invoice.is_(False),
        Ticket.invoice_id.is_(None),
    ]
    if date_from:
        ticket_filters.append(
            Ticket.datetime >= datetime.combine(date_from, time.min)
        )
    if date_to:
        ticket_filters.append(
            Ticket.datetime <= datetime.combine(date_to, time.max)
        )

    ticket_rows = db.execute(
        select(Ticket, Product, TaxRate)
        .join(Product, Ticket.product_id == Product.id)
        .outerjoin(TaxRate, Product.tax_rate_id == TaxRate.id)
        .where(and_(*ticket_filters))
        .order_by(Ticket.datetime.asc())
    ).all()

    if not ticket_rows:
        customers = db.execute(select(Customer).order_by(Customer.name)).scalars().all()
        return templates.TemplateResponse(
            "invoices/generate.html",
            {
                "request": request,
                "errors": ["No eligible tickets found for that range."],
                "customers": customers,
                "form": {
                    "customer_id": str(customer_id or ""),
                    "date_from": date_from_raw,
                    "date_to": date_to_raw,
                },
            },
            status_code=400,
        )

    invoice = Invoice(
        invoice_no=_generate_invoice_no(db),
        customer_id=customer_id,
        invoice_date=date.today(),
        status="DRAFT",
        net_total=0,
        vat_total=0,
        gross_total=0,
    )
    db.add(invoice)
    db.flush()

    net_total = 0.0
    vat_total = 0.0

    for ticket, product, tax_rate in ticket_rows:
        net = float(ticket.total or 0)
        rate = float(tax_rate.rate_percent or 0) if tax_rate else 0.0
        vat = round(net * rate / 100, 2)
        gross = net + vat

        line = InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket.id,
            description=f"Ticket {ticket.ticket_no} - {product.description}",
            quantity=float(ticket.qty or 0),
            unit_price=float(ticket.unit_price or 0),
            net=net,
            vat=vat,
            gross=gross,
        )
        db.add(line)
        ticket.invoice_id = invoice.id
        net_total += net
        vat_total += vat

    invoice.net_total = round(net_total, 2)
    invoice.vat_total = round(vat_total, 2)
    invoice.gross_total = round(net_total + vat_total, 2)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoices_detail(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )
    customer = db.get(Customer, invoice.customer_id)
    lines = db.execute(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice.id)
        .order_by(InvoiceLine.id)
    ).scalars().all()
    tickets = db.execute(
        select(Ticket).where(Ticket.invoice_id == invoice.id).order_by(Ticket.datetime)
    ).scalars().all()
    payment_methods = db.execute(
        select(PaymentMethod).order_by(PaymentMethod.code)
    ).scalars().all()
    return templates.TemplateResponse(
        "invoices/detail.html",
        {
            "request": request,
            "invoice": invoice,
            "customer": customer,
            "lines": lines,
            "tickets": tickets,
            "payment_methods": payment_methods,
            "errors": [],
        },
    )


@router.post("/invoices/{invoice_id}/paid", response_class=HTMLResponse)
async def invoices_mark_paid(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            "invoices/not_found.html",
            {"request": request, "invoice_id": invoice_id},
            status_code=404,
        )

    form = await request.form()
    payment_method_id = _parse_int(str(form.get("payment_method_id", "")).strip())
    paid_at_raw = str(form.get("paid_at", "")).strip()

    if not payment_method_id or not paid_at_raw:
        payment_methods = db.execute(
            select(PaymentMethod).order_by(PaymentMethod.code)
        ).scalars().all()
        customer = db.get(Customer, invoice.customer_id)
        lines = db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
            .order_by(InvoiceLine.id)
        ).scalars().all()
        tickets = db.execute(
            select(Ticket)
            .where(Ticket.invoice_id == invoice.id)
            .order_by(Ticket.datetime)
        ).scalars().all()
        return templates.TemplateResponse(
            "invoices/detail.html",
            {
                "request": request,
                "invoice": invoice,
                "customer": customer,
                "lines": lines,
                "tickets": tickets,
                "payment_methods": payment_methods,
                "errors": ["Payment method and paid date are required."],
            },
            status_code=400,
        )

    try:
        paid_at = datetime.fromisoformat(paid_at_raw)
    except ValueError:
        paid_at = None

    if not paid_at:
        payment_methods = db.execute(
            select(PaymentMethod).order_by(PaymentMethod.code)
        ).scalars().all()
        customer = db.get(Customer, invoice.customer_id)
        lines = db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
            .order_by(InvoiceLine.id)
        ).scalars().all()
        tickets = db.execute(
            select(Ticket)
            .where(Ticket.invoice_id == invoice.id)
            .order_by(Ticket.datetime)
        ).scalars().all()
        return templates.TemplateResponse(
            "invoices/detail.html",
            {
                "request": request,
                "invoice": invoice,
                "customer": customer,
                "lines": lines,
                "tickets": tickets,
                "payment_methods": payment_methods,
                "errors": ["Paid date must be valid."],
            },
            status_code=400,
        )

    invoice.status = "PAID"
    invoice.payment_method_id = payment_method_id
    invoice.paid_at = paid_at
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)


def _generate_invoice_no(db: Session) -> str:
    while True:
        stamp = datetime.utcnow().strftime("INV%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:4].upper()
        invoice_no = f"{stamp}-{suffix}"
        exists = db.execute(
            select(Invoice.id).where(Invoice.invoice_no == invoice_no)
        ).first()
        if not exists:
            return invoice_no


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
