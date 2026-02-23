from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..constants import (
    ADDRESS_LINE_MAX,
    CODE_MAX,
    DESC_MAX,
    NAME_MAX,
    POSTCODE_MAX,
)
from ..db import get_db
from ..security import validate_no_html_fields
from ..models.base import utcnow
from ..models import Customer, CustomerProductPrice, Product, Unit
from ..templating import templates

router = APIRouter()
ACCOUNT_CODE_MAX_LEN = CODE_MAX
ACCOUNT_CODE_RE = re.compile(r"^[A-Z0-9-]+$")
INVOICE_FREQUENCIES = ("WEEKLY", "MONTHLY", "ADHOC")
INVOICE_FREQUENCY_LABELS = {
    "WEEKLY": "Weekly",
    "MONTHLY": "Monthly",
    "ADHOC": "Adhoc",
}
OVERRIDE_PRICE_MAX = Decimal("9999999999.99")


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
    return templates.TemplateResponse(request, 
        "customers/list.html",
        {
            "request": request,
            "customers": customers,
            "q": q or "",
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/customers/new", response_class=HTMLResponse)
def customers_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(request, 
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
        return templates.TemplateResponse(request, 
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
        credit_limit_pence=payload["credit_limit_pence"],
        is_cash_account=payload["is_cash_account"],
        payment_terms=payload["payment_terms"],
        invoice_frequency=payload["invoice_frequency"],
        payment_terms_days=payload["payment_terms_days"],
        on_stop=payload["on_stop"],
        do_not_invoice=payload["do_not_invoice"],
        must_have_po=payload["must_have_po"],
    )
    db.add(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Account code already exists.")
        return templates.TemplateResponse(request, 
            "customers/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/customers?saved=1", status_code=303)


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customers_edit(
    customer_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(request, 
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "customers/edit.html",
        {
            "request": request,
            "errors": [],
            "saved": request.query_params.get("saved") == "1",
            "customer": customer,
            "form": _customer_to_form(customer),
            "options": _load_options(db),
            "price_overrides": _customer_price_overrides(db, customer.id),
            "override_form": _empty_override_form(),
        },
    )


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
async def customers_update(
    customer_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(request, 
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_customer_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            request,
            "customers/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "customer": customer,
                "form": payload["form"],
                "options": _load_options(db),
                "price_overrides": _customer_price_overrides(db, customer.id),
                "override_form": _empty_override_form(),
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
    customer.credit_limit_pence = payload["credit_limit_pence"]
    customer.is_cash_account = payload["is_cash_account"]
    if payload["payment_terms_provided"]:
        customer.payment_terms = payload["payment_terms"]
    customer.invoice_frequency = payload["invoice_frequency"]
    customer.payment_terms_days = payload["payment_terms_days"]
    customer.on_stop = payload["on_stop"]
    customer.do_not_invoice = payload["do_not_invoice"]
    customer.must_have_po = payload["must_have_po"]
    customer.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Account code already exists.")
        return templates.TemplateResponse(
            request,
            "customers/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "customer": customer,
                "form": payload["form"],
                "options": _load_options(db),
                "price_overrides": _customer_price_overrides(db, customer.id),
                "override_form": _empty_override_form(),
            },
            status_code=400,
        )
    return RedirectResponse(url="/customers?saved=1", status_code=303)


@router.post("/customers/{customer_id}/price-overrides", response_class=HTMLResponse)
async def customer_price_override_create(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            request,
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_override_form(form)
    product = _validate_override_product(
        db,
        payload.get("product_id"),
        errors=payload["errors"],
    )
    if payload.get("product_id") and _has_active_override(
        db,
        customer_id=customer.id,
        product_id=payload["product_id"],
    ):
        payload["errors"].append(
            "Active override already exists for this customer and product."
        )

    if payload["errors"]:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            override_form=payload["form"],
            status_code=400,
        )

    override = CustomerProductPrice(
        customer_id=customer.id,
        product_id=product.id,
        unit_price=payload["unit_price"],
        is_active=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(override)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=["Active override already exists for this customer and product."],
            override_form=payload["form"],
            status_code=400,
        )
    return RedirectResponse(url=f"/customers/{customer.id}?saved=1", status_code=303)


@router.post(
    "/customers/{customer_id}/price-overrides/{override_id}/update",
    response_class=HTMLResponse,
)
async def customer_price_override_update(
    customer_id: int,
    override_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            request,
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )
    override = db.get(CustomerProductPrice, override_id)
    if not override or override.customer_id != customer.id:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=["Price override not found."],
            status_code=404,
        )

    form = await request.form()
    payload = _parse_override_form(form)
    product_id = payload["product_id"] if payload.get("product_id") else override.product_id
    product = _validate_override_product(
        db,
        product_id,
        errors=payload["errors"],
        current_product_id=override.product_id,
    )
    new_active = payload["is_active"]
    if (
        new_active
        and product_id
        and _has_active_override(
            db,
            customer_id=customer.id,
            product_id=product_id,
            exclude_override_id=override.id,
        )
    ):
        payload["errors"].append(
            "Active override already exists for this customer and product."
        )

    if payload["errors"]:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            override_form=payload["form"],
            status_code=400,
        )

    override.product_id = product.id if product else override.product_id
    override.unit_price = payload["unit_price"]
    override.is_active = new_active
    override.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=["Active override already exists for this customer and product."],
            override_form=payload["form"],
            status_code=400,
        )
    return RedirectResponse(url=f"/customers/{customer.id}?saved=1", status_code=303)


@router.post(
    "/customers/{customer_id}/price-overrides/{override_id}/deactivate",
    response_class=HTMLResponse,
)
def customer_price_override_deactivate(
    customer_id: int,
    override_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            request,
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )
    override = db.get(CustomerProductPrice, override_id)
    if not override or override.customer_id != customer.id:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=["Price override not found."],
            status_code=404,
        )
    override.is_active = False
    override.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}?saved=1", status_code=303)


def _load_options(db: Session) -> dict[str, list[tuple[str, str]]]:
    override_products = (
        db.execute(
            select(Product)
            .options(joinedload(Product.unit))
            .join(Unit, Product.unit_id == Unit.id)
            .where(Unit.is_active.is_(True))
            .order_by(Product.code.asc(), Product.description.asc())
        )
        .scalars()
        .all()
    )
    return {
        "invoice_frequencies": [
            (value, INVOICE_FREQUENCY_LABELS.get(value, value.title()))
            for value in INVOICE_FREQUENCIES
        ],
        "override_products": [
            (str(product.id), _product_override_label(product))
            for product in override_products
        ],
    }


def _empty_override_form() -> dict[str, str]:
    return {
        "product_id": "",
        "unit_price": "",
        "is_active": "on",
    }


def _parse_override_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    def checkbox_checked(key: str) -> bool:
        if hasattr(form, "getlist"):
            values = [str(item).strip().lower() for item in form.getlist(key)]
            return any(item in {"on", "1", "true", "yes"} for item in values)
        return value(key).lower() in {"on", "1", "true", "yes"}

    errors: list[str] = []
    product_id_raw = value("product_id")
    unit_price_raw = value("unit_price")
    is_active = checkbox_checked("is_active")

    validate_no_html_fields(
        {
            "Product": product_id_raw,
            "Unit price": unit_price_raw,
        },
        errors,
    )

    product_id = _parse_int(product_id_raw)
    if not product_id_raw:
        errors.append("Product is required.")
    elif product_id is None:
        errors.append("Product is invalid.")

    unit_price = _parse_decimal(unit_price_raw)
    if not unit_price_raw:
        errors.append("Override price is required.")
    elif unit_price is None:
        errors.append("Override price must be a number.")
    elif unit_price < 0:
        errors.append("Override price must be 0 or greater.")
    elif unit_price > OVERRIDE_PRICE_MAX:
        errors.append("Override price is too large.")

    return {
        "errors": errors,
        "form": {
            "product_id": product_id_raw,
            "unit_price": unit_price_raw,
            "is_active": "on" if is_active else "off",
        },
        "product_id": product_id,
        "unit_price": unit_price,
        "is_active": is_active,
    }


def _customer_price_overrides(db: Session, customer_id: int) -> list[CustomerProductPrice]:
    return (
        db.execute(
            select(CustomerProductPrice)
            .options(joinedload(CustomerProductPrice.product).joinedload(Product.unit))
            .where(CustomerProductPrice.customer_id == customer_id)
            .order_by(
                CustomerProductPrice.is_active.desc(),
                CustomerProductPrice.updated_at.desc(),
                CustomerProductPrice.id.desc(),
            )
        )
        .scalars()
        .all()
    )


def _validate_override_product(
    db: Session,
    product_id: int | None,
    *,
    errors: list[str],
    current_product_id: int | None = None,
) -> Product | None:
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
        errors.append("Product not found.")
        return None
    if (
        (product.unit is None or not product.unit.is_active)
        and product.id != current_product_id
    ):
        errors.append("Product is inactive.")
        return None
    return product


def _has_active_override(
    db: Session,
    *,
    customer_id: int,
    product_id: int,
    exclude_override_id: int | None = None,
) -> bool:
    query = select(CustomerProductPrice.id).where(
        and_(
            CustomerProductPrice.customer_id == customer_id,
            CustomerProductPrice.product_id == product_id,
            CustomerProductPrice.is_active.is_(True),
        )
    )
    if exclude_override_id is not None:
        query = query.where(CustomerProductPrice.id != exclude_override_id)
    existing = db.execute(query.limit(1)).scalar_one_or_none()
    return existing is not None


def _product_override_label(product: Product) -> str:
    unit = product.unit
    unit_type = unit.unit_type if unit else "COUNT"
    return (
        f"{product.code} - {product.description}"
        f" ({unit_type})"
    )


def _render_customer_edit_with_overrides(
    request: Request,
    db: Session,
    customer: Customer,
    *,
    errors: list[str],
    form: dict | None = None,
    override_form: dict | None = None,
    status_code: int = 400,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "customers/edit.html",
        {
            "request": request,
            "errors": errors,
            "customer": customer,
            "form": form or _customer_to_form(customer),
            "options": _load_options(db),
            "price_overrides": _customer_price_overrides(db, customer.id),
            "override_form": override_form or _empty_override_form(),
        },
        status_code=status_code,
    )


def _parse_customer_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    account_code = value("account_code").upper()
    name = value("name")
    invoice_email = value("invoice_email")
    phone = value("phone")
    address_line1 = value("address_line1")
    address_line2 = value("address_line2")
    city = value("city")
    postcode = value("postcode")
    country = value("country")
    vat_number = value("vat_number")
    payment_terms_provided = "payment_terms" in form
    payment_terms = value("payment_terms")
    invoice_frequency_raw = value("invoice_frequency").upper()
    payment_terms_days_raw = value("payment_terms_days")
    credit_limit_pounds_raw = value("credit_limit_pounds") or value("credit_limit")

    validate_no_html_fields(
        {
            "Account code": account_code,
            "Name": name,
            "Invoice email": invoice_email,
            "Phone": phone,
            "Address line 1": address_line1,
            "Address line 2": address_line2,
            "City": city,
            "Postcode": postcode,
            "Country": country,
            "VAT number": vat_number,
            "Payment terms": payment_terms,
            "Invoice frequency": invoice_frequency_raw,
            "Payment terms (days)": payment_terms_days_raw,
            "Credit limit": credit_limit_pounds_raw,
        },
        errors,
    )

    payment_terms_days = _parse_int(payment_terms_days_raw)
    credit_limit_pence = _parse_money_to_pence(credit_limit_pounds_raw)
    if payment_terms_days_raw and payment_terms_days is None:
        errors.append("Payment terms (days) must be a whole number.")
    elif payment_terms_days is not None and payment_terms_days < 0:
        errors.append("Payment terms (days) cannot be negative.")
    if credit_limit_pounds_raw and credit_limit_pence is None:
        errors.append("Credit limit must be a valid amount.")
    elif credit_limit_pence is not None and credit_limit_pence < 0:
        errors.append("Credit limit cannot be negative.")
    if invoice_frequency_raw and invoice_frequency_raw not in INVOICE_FREQUENCIES:
        errors.append("Invoice frequency must be WEEKLY, MONTHLY, or ADHOC.")

    if not account_code:
        errors.append("Account code is required.")
    elif len(account_code) > ACCOUNT_CODE_MAX_LEN:
        errors.append(
            f"Account code must be {ACCOUNT_CODE_MAX_LEN} characters or fewer."
        )
    elif not ACCOUNT_CODE_RE.fullmatch(account_code):
        errors.append("Account code can only contain A-Z, 0-9, and -.")
    if not name:
        errors.append("Name is required.")
    elif len(name) > NAME_MAX:
        errors.append(f"Name must be {NAME_MAX} characters or fewer.")
    if invoice_email and len(invoice_email) > DESC_MAX:
        errors.append(f"Invoice email must be {DESC_MAX} characters or fewer.")
    if phone and len(phone) > CODE_MAX:
        errors.append(f"Phone must be {CODE_MAX} characters or fewer.")
    if address_line1 and len(address_line1) > ADDRESS_LINE_MAX:
        errors.append(f"Address line 1 must be {ADDRESS_LINE_MAX} characters or fewer.")
    if address_line2 and len(address_line2) > ADDRESS_LINE_MAX:
        errors.append(f"Address line 2 must be {ADDRESS_LINE_MAX} characters or fewer.")
    if city and len(city) > NAME_MAX:
        errors.append(f"City must be {NAME_MAX} characters or fewer.")
    if postcode and len(postcode) > POSTCODE_MAX:
        errors.append(f"Postcode must be {POSTCODE_MAX} characters or fewer.")
    if country and len(country) > NAME_MAX:
        errors.append(f"Country must be {NAME_MAX} characters or fewer.")
    if vat_number and len(vat_number) > CODE_MAX:
        errors.append(f"VAT number must be {CODE_MAX} characters or fewer.")
    if payment_terms and len(payment_terms) > NAME_MAX:
        errors.append(f"Payment terms must be {NAME_MAX} characters or fewer.")

    return {
        "errors": errors,
        "form": {
            "account_code": account_code,
            "name": name,
            "invoice_email": invoice_email,
            "phone": phone,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "city": city,
            "postcode": postcode,
            "country": country,
            "vat_number": vat_number,
            "payment_terms": payment_terms,
            "invoice_frequency_id": value("invoice_frequency_id"),
            "invoice_frequency": invoice_frequency_raw,
            "payment_terms_days": payment_terms_days_raw,
            "credit_limit_pounds": credit_limit_pounds_raw,
            "credit_limit": value("credit_limit"),
            "on_stop": value("on_stop"),
            "is_cash_account": value("is_cash_account"),
            "cash_account": value("cash_account"),
            "do_not_invoice": value("do_not_invoice"),
            "must_have_po": value("must_have_po"),
        },
        "account_code": account_code,
        "name": name,
        "invoice_email": invoice_email or None,
        "phone": phone or None,
        "address_line1": address_line1 or None,
        "address_line2": address_line2 or None,
        "city": city or None,
        "postcode": postcode or None,
        "country": country or None,
        "vat_number": vat_number or None,
        "payment_terms_provided": payment_terms_provided,
        "payment_terms": payment_terms or None,
        "invoice_frequency_id": _parse_int(value("invoice_frequency_id")),
        "invoice_frequency": invoice_frequency_raw or None,
        "payment_terms_days": payment_terms_days,
        "credit_limit_pence": credit_limit_pence,
        "is_cash_account": value("is_cash_account") == "on",
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
        "payment_terms": "",
        "invoice_frequency_id": "",
        "invoice_frequency": "",
        "payment_terms_days": "",
        "credit_limit_pounds": "",
        "credit_limit": "",
        "on_stop": "",
        "is_cash_account": "",
        "cash_account": "",
        "do_not_invoice": "",
        "must_have_po": "",
    }


def _customer_to_form(customer: Customer) -> dict:
    return {
        "account_code": (customer.account_code or "").upper(),
        "name": customer.name or "",
        "invoice_email": customer.invoice_email or "",
        "phone": customer.phone or "",
        "address_line1": customer.address_line1 or "",
        "address_line2": customer.address_line2 or "",
        "city": customer.city or "",
        "postcode": customer.postcode or "",
        "country": customer.country or "",
        "vat_number": customer.vat_number or "",
        "payment_terms": customer.payment_terms or "",
        "invoice_frequency_id": str(customer.invoice_frequency_id or ""),
        "invoice_frequency": customer.invoice_frequency or "",
        "payment_terms_days": (
            str(customer.payment_terms_days)
            if customer.payment_terms_days is not None
            else ""
        ),
        "credit_limit_pounds": _format_pence_as_money(customer.credit_limit_pence),
        "credit_limit": _format_decimal(customer.credit_limit),
        "on_stop": "on" if customer.on_stop else "",
        "is_cash_account": "on" if customer.is_cash_account else "",
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


def _parse_money_to_pence(value: str) -> int | None:
    if not value:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def _format_pence_as_money(value: int | None) -> str:
    if value is None:
        return ""
    return f"{(Decimal(value) / Decimal(100)):.2f}"
