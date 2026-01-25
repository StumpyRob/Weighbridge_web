from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Customer, InvoiceFrequency

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/customers", response_class=HTMLResponse)
def customers_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = select(Customer).order_by(Customer.name)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Customer.name.ilike(like), Customer.account_code.ilike(like))
        )
    customers = db.execute(query).scalars().all()
    return templates.TemplateResponse(
        "customers/list.html",
        {"request": request, "customers": customers, "q": q or ""},
    )


@router.get("/customers/new", response_class=HTMLResponse)
def customers_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "customers/new.html",
        {
            "request": request,
            "errors": [],
            "form": _empty_form(),
            "options": _load_options(db),
        },
    )


@router.post("/customers/new", response_class=HTMLResponse)
async def customers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_customer_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "customers/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )

    customer = Customer(
        account_code=payload["account_code"],
        name=payload["name"],
        invoice_email=payload["invoice_email"],
        phone=payload["phone"],
        address_line1=payload["address_line1"],
        address_line2=payload["address_line2"],
        city=payload["city"],
        postcode=payload["postcode"],
        country=payload["country"],
        vat_number=payload["vat_number"],
        invoice_frequency_id=payload["invoice_frequency_id"],
        payment_terms=payload["payment_terms"],
        credit_limit=payload["credit_limit"],
        on_stop=payload["on_stop"],
        cash_account=payload["cash_account"],
        do_not_invoice=payload["do_not_invoice"],
        must_have_po=payload["must_have_po"],
    )
    db.add(customer)
    db.commit()
    return RedirectResponse(url="/customers", status_code=303)


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customers_edit(
    customer_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        "customers/edit.html",
        {
            "request": request,
            "errors": [],
            "customer": customer,
            "form": _customer_to_form(customer),
            "options": _load_options(db),
        },
    )


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
async def customers_update(
    customer_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_customer_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "customers/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "customer": customer,
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )

    customer.account_code = payload["account_code"]
    customer.name = payload["name"]
    customer.invoice_email = payload["invoice_email"]
    customer.phone = payload["phone"]
    customer.address_line1 = payload["address_line1"]
    customer.address_line2 = payload["address_line2"]
    customer.city = payload["city"]
    customer.postcode = payload["postcode"]
    customer.country = payload["country"]
    customer.vat_number = payload["vat_number"]
    customer.invoice_frequency_id = payload["invoice_frequency_id"]
    customer.payment_terms = payload["payment_terms"]
    customer.credit_limit = payload["credit_limit"]
    customer.on_stop = payload["on_stop"]
    customer.cash_account = payload["cash_account"]
    customer.do_not_invoice = payload["do_not_invoice"]
    customer.must_have_po = payload["must_have_po"]
    customer.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}", status_code=303)


def _load_options(db: Session) -> dict[str, list[tuple[str, str]]]:
    frequencies = db.execute(
        select(InvoiceFrequency).order_by(InvoiceFrequency.code)
    ).scalars()
    return {
        "invoice_frequencies": [(str(row.id), row.code) for row in frequencies],
    }


def _parse_customer_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    account_code = value("account_code")
    name = value("name")
    if not account_code:
        errors.append("Account code is required.")
    if not name:
        errors.append("Name is required.")

    return {
        "errors": errors,
        "form": {
            "account_code": account_code,
            "name": name,
            "invoice_email": value("invoice_email"),
            "phone": value("phone"),
            "address_line1": value("address_line1"),
            "address_line2": value("address_line2"),
            "city": value("city"),
            "postcode": value("postcode"),
            "country": value("country"),
            "vat_number": value("vat_number"),
            "invoice_frequency_id": value("invoice_frequency_id"),
            "payment_terms": value("payment_terms"),
            "credit_limit": value("credit_limit"),
            "on_stop": value("on_stop"),
            "cash_account": value("cash_account"),
            "do_not_invoice": value("do_not_invoice"),
            "must_have_po": value("must_have_po"),
        },
        "account_code": account_code,
        "name": name,
        "invoice_email": value("invoice_email") or None,
        "phone": value("phone") or None,
        "address_line1": value("address_line1") or None,
        "address_line2": value("address_line2") or None,
        "city": value("city") or None,
        "postcode": value("postcode") or None,
        "country": value("country") or None,
        "vat_number": value("vat_number") or None,
        "invoice_frequency_id": _parse_int(value("invoice_frequency_id")),
        "payment_terms": value("payment_terms") or None,
        "credit_limit": _parse_decimal(value("credit_limit")),
        "on_stop": value("on_stop") == "on",
        "cash_account": value("cash_account") == "on",
        "do_not_invoice": value("do_not_invoice") == "on",
        "must_have_po": value("must_have_po") == "on",
    }


def _empty_form() -> dict:
    return {
        "account_code": "",
        "name": "",
        "invoice_email": "",
        "phone": "",
        "address_line1": "",
        "address_line2": "",
        "city": "",
        "postcode": "",
        "country": "",
        "vat_number": "",
        "invoice_frequency_id": "",
        "payment_terms": "",
        "credit_limit": "",
        "on_stop": "",
        "cash_account": "",
        "do_not_invoice": "",
        "must_have_po": "",
    }


def _customer_to_form(customer: Customer) -> dict:
    return {
        "account_code": customer.account_code or "",
        "name": customer.name or "",
        "invoice_email": customer.invoice_email or "",
        "phone": customer.phone or "",
        "address_line1": customer.address_line1 or "",
        "address_line2": customer.address_line2 or "",
        "city": customer.city or "",
        "postcode": customer.postcode or "",
        "country": customer.country or "",
        "vat_number": customer.vat_number or "",
        "invoice_frequency_id": str(customer.invoice_frequency_id or ""),
        "payment_terms": customer.payment_terms or "",
        "credit_limit": _format_decimal(customer.credit_limit),
        "on_stop": "on" if customer.on_stop else "",
        "cash_account": "on" if customer.cash_account else "",
        "do_not_invoice": "on" if customer.do_not_invoice else "",
        "must_have_po": "on" if customer.must_have_po else "",
    }


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
