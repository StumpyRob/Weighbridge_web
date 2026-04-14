from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..auth import is_admin_user, user_display_name
from ..constants import (
    ADDRESS_LINE_MAX,
    CODE_MAX,
    DESC_MAX,
    NAME_MAX,
    NOTES_MAX,
    POSTCODE_MAX,
)
from ..db import get_db
from ..permissions import (
    PERM_MANAGE_CUSTOMERS,
    PERM_VIEW_CUSTOMERS,
    require_permission,
)
from ..security import validate_no_html_fields
from ..models.base import utcnow
from ..models import (
    Customer,
    CustomerAdjustment,
    CustomerProductPrice,
    Product,
    Unit,
)
from ..services.credit import (
    customer_adjustments_total,
    customer_invoice_outstanding_total,
    customer_outstanding_total,
    money_decimal,
    outstanding_display_values,
)
from ..services.edit_conflicts import (
    ROW_VERSION_FIELD,
    STALE_EDIT_MESSAGE,
    row_version_conflict,
    row_version_token,
)
from ..templating import templates

router = APIRouter()
ACCOUNT_CODE_MAX_LEN = CODE_MAX
ACCOUNT_CODE_RE = re.compile(r"^[A-Z0-9-]+$")
ACCOUNT_CODE_SANITIZE_RE = re.compile(r"[^A-Z0-9-]+")
PHONE_SANITIZE_RE = re.compile(r"[^0-9]+")
POSTCODE_SANITIZE_RE = re.compile(r"[^A-Z0-9\s]+")
INVOICE_FREQUENCIES = ("WEEKLY", "MONTHLY", "ADHOC")
INVOICE_FREQUENCY_LABELS = {
    "WEEKLY": "Weekly",
    "MONTHLY": "Monthly",
    "ADHOC": "Adhoc",
}
OVERRIDE_PRICE_MAX = Decimal("9999999999.99")
ADJUSTMENT_AMOUNT_MAX = Decimal("9999999999.99")
ADJUSTMENT_REASONS: tuple[tuple[str, str], ...] = (
    ("GOODWILL_CREDIT", "Goodwill credit"),
    ("PRICING_DISPUTE", "Pricing dispute"),
    ("WRITE_OFF", "Write-off"),
    ("MANUAL_CORRECTION", "Manual correction"),
    ("OTHER", "Other"),
)
CREDIT_AVAILABLE_LOW_RATIO = Decimal("0.20")


def _forbidden_response() -> HTMLResponse:
    return HTMLResponse("Forbidden", status_code=403)


def _normalize_account_code(value: str) -> str:
    return ACCOUNT_CODE_SANITIZE_RE.sub("", str(value or "").upper())


def _normalize_phone(value: str) -> str:
    return PHONE_SANITIZE_RE.sub("", str(value or ""))


def _normalize_postcode(value: str) -> str:
    sanitized = POSTCODE_SANITIZE_RE.sub("", str(value or "").upper())
    return " ".join(sanitized.split())


def _resolved_tenant_id(
    request: Request,
    db: Session,
    *,
    fallback_tenant_id: int | None = None,
) -> int | None:
    tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
    if tenant_id is None:
        tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        tenant_id = fallback_tenant_id
    return int(tenant_id) if tenant_id is not None else None


def _customer_account_code_exists(
    db: Session,
    account_code: str,
    *,
    tenant_id: int | None,
    exclude_customer_id: int | None = None,
) -> bool:
    query = (
        select(Customer.id)
        .execution_options(skip_tenant_scope=True)
        .where(Customer.account_code == account_code)
    )
    if tenant_id is not None:
        query = query.where(Customer.tenant_id == int(tenant_id))
    if exclude_customer_id is not None:
        query = query.where(Customer.id != int(exclude_customer_id))
    return db.execute(query.limit(1)).scalar_one_or_none() is not None


def _current_user_is_admin(request: Request, db: Session) -> bool:
    return is_admin_user(db, getattr(request.state, "current_user", None))


def _customer_admin_controls_requested(payload: dict[str, object]) -> bool:
    credit_limit_pence = payload.get("credit_limit_pence")
    try:
        has_credit_limit = int(credit_limit_pence or 0) > 0
    except (TypeError, ValueError):
        has_credit_limit = False
    return bool(
        payload.get("on_stop")
        or payload.get("do_not_invoice")
        or payload.get("must_have_po")
        or has_credit_limit
    )


@router.get("/customers", response_class=HTMLResponse)
def customers_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_VIEW_CUSTOMERS)
    active_price_override_count = (
        select(func.count(CustomerProductPrice.id))
        .where(
            CustomerProductPrice.customer_id == Customer.id,
            CustomerProductPrice.is_active.is_(True),
        )
        .scalar_subquery()
    )
    query = (
        select(
            Customer,
            active_price_override_count.label("active_price_override_count"),
        )
        .order_by(Customer.name)
    )
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Customer.name.ilike(like), Customer.account_code.ilike(like))
        )
    rows = db.execute(query).all()
    return templates.TemplateResponse(request, 
        "customers/list.html",
        {
            "request": request,
            "rows": rows,
            "q": q or "",
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/customers/new", response_class=HTMLResponse)
def customers_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_CUSTOMERS)
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
    require_permission(request, PERM_MANAGE_CUSTOMERS)
    form = await request.form()
    payload = _parse_customer_form(form)
    tenant_id = _resolved_tenant_id(request, db)
    if _customer_admin_controls_requested(payload) and not _current_user_is_admin(request, db):
        return _forbidden_response()
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
    if _customer_account_code_exists(
        db,
        payload["account_code"],
        tenant_id=tenant_id,
    ):
        payload["errors"].append("Account code already exists.")
        return templates.TemplateResponse(
            request,
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
        db.flush()
        audit_log(
            db,
            request,
            action="CREATE",
            entity_type="customer",
            entity_id=customer.id,
            summary=f"Created customer {customer.account_code}",
            details={
                "account_code": customer.account_code,
                "name": customer.name,
            },
        )
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
    require_permission(request, PERM_VIEW_CUSTOMERS)
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(request, 
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )
    return _render_customer_edit_with_overrides(
        request,
        db,
        customer,
        errors=[],
        saved=request.query_params.get("saved") == "1",
        adjustment_saved=request.query_params.get("adjustment_saved") == "1",
        status_code=200,
    )


@router.post("/customers/{customer_id}", response_class=HTMLResponse)
async def customers_update(
    customer_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_CUSTOMERS)
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(request, 
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_customer_form(form)
    tenant_id = _resolved_tenant_id(
        request,
        db,
        fallback_tenant_id=int(customer.tenant_id or 0) or None,
    )
    before_audit = {
        "on_stop": bool(customer.on_stop),
        "dont_invoice": bool(customer.do_not_invoice),
        "po_required": bool(customer.must_have_po),
        "credit_limit": customer.credit_limit_pence,
        "phone": customer.phone,
        "email": customer.invoice_email,
    }
    admin_control_change = bool(
        before_audit["on_stop"] != bool(payload["on_stop"])
        or before_audit["dont_invoice"] != bool(payload["do_not_invoice"])
        or before_audit["po_required"] != bool(payload["must_have_po"])
        or int(before_audit["credit_limit"] or 0) != int(payload["credit_limit_pence"] or 0)
    )
    if admin_control_change and not _current_user_is_admin(request, db):
        return _forbidden_response()
    if payload["errors"]:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            form=payload["form"],
            status_code=400,
        )
    if _customer_account_code_exists(
        db,
        payload["account_code"],
        tenant_id=tenant_id,
        exclude_customer_id=customer.id,
    ):
        payload["errors"].append("Account code already exists.")
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            form=payload["form"],
            status_code=400,
        )
    if row_version_conflict(customer, form.get(ROW_VERSION_FIELD)):
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=[STALE_EDIT_MESSAGE],
            form=payload["form"],
            status_code=409,
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

    after_audit = {
        "on_stop": bool(customer.on_stop),
        "dont_invoice": bool(customer.do_not_invoice),
        "po_required": bool(customer.must_have_po),
        "credit_limit": customer.credit_limit_pence,
        "phone": customer.phone,
        "email": customer.invoice_email,
    }
    change_details = audit_diff(
        before_audit,
        after_audit,
        ["on_stop", "dont_invoice", "po_required", "credit_limit", "phone", "email"],
    )
    if change_details["changed"]:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="customer",
            entity_id=customer.id,
            summary=f"Updated customer {customer.account_code}",
            details=change_details,
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Account code already exists.")
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            form=payload["form"],
            status_code=400,
        )
    return RedirectResponse(url="/customers?saved=1", status_code=303)


@router.post("/customers/{customer_id}/price-overrides", response_class=HTMLResponse)
async def customer_price_override_create(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_CUSTOMERS)
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
        db.flush()
        audit_log(
            db,
            request,
            action="CREATE",
            entity_type="price_override",
            entity_id=override.id,
            summary=f"Created price override for customer {customer.account_code}",
            details={
                "customer_id": customer.id,
                "customer_account_code": customer.account_code,
                "product_id": product.id,
                "product_code": str(product.code or "").strip() or None,
                "unit_price": str(payload["unit_price"]),
                "is_active": True,
            },
        )
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
    require_permission(request, PERM_MANAGE_CUSTOMERS)
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

    before_product = db.get(Product, override.product_id) if override.product_id else None
    before_audit = {
        "product_id": override.product_id,
        "product_code": (
            str(before_product.code or "").strip() or None if before_product else None
        ),
        "unit_price": override.unit_price,
        "is_active": bool(override.is_active),
    }
    override.product_id = product.id if product else override.product_id
    override.unit_price = payload["unit_price"]
    override.is_active = new_active
    override.updated_at = utcnow()
    if product:
        after_product_code = str(product.code or "").strip() or None
    else:
        existing_product = db.get(Product, override.product_id) if override.product_id else None
        after_product_code = (
            str(existing_product.code or "").strip() or None if existing_product else None
        )
    after_audit = {
        "product_id": override.product_id,
        "unit_price": override.unit_price,
        "is_active": bool(override.is_active),
        "product_code": after_product_code,
    }
    change_details = audit_diff(
        before_audit,
        after_audit,
        ["product_id", "product_code", "unit_price", "is_active"],
    )
    if change_details["changed"]:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="price_override",
            entity_id=override.id,
            summary=f"Updated price override for customer {customer.account_code}",
            details=change_details,
        )
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
    require_permission(request, PERM_MANAGE_CUSTOMERS)
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
    product = db.get(Product, override.product_id) if override.product_id else None
    override.is_active = False
    override.updated_at = utcnow()
    audit_log(
        db,
        request,
        action="DEACTIVATE",
        entity_type="price_override",
        entity_id=override.id,
        summary=f"Deactivated price override for customer {customer.account_code}",
        details={
            "customer_id": customer.id,
            "customer_account_code": customer.account_code,
            "product_id": override.product_id,
            "product_code": str(product.code or "").strip() or None if product else None,
        },
    )
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}?saved=1", status_code=303)


@router.post("/customers/{customer_id}/adjustments", response_class=HTMLResponse)
async def customer_adjustment_create(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_CUSTOMERS)
    customer = db.get(Customer, customer_id)
    if not customer:
        return templates.TemplateResponse(
            request,
            "customers/not_found.html",
            {"request": request, "customer_id": customer_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_adjustment_form(form)
    if payload["errors"]:
        return _render_customer_edit_with_overrides(
            request,
            db,
            customer,
            errors=payload["errors"],
            adjustment_form=payload["form"],
            status_code=400,
        )

    adjustment = CustomerAdjustment(
        customer_id=customer.id,
        amount_decimal=payload["amount_decimal"],
        reason=payload["reason"],
        note=payload["note"],
        created_by_user_id=None,
        created_at=utcnow(),
    )
    db.add(adjustment)
    db.flush()
    actor = user_display_name(getattr(request.state, "current_user", None))
    audit_log(
        db,
        request,
        action="CREATE",
        entity_type="credit_adjustment",
        entity_id=adjustment.id,
        summary=f"Added credit adjustment for customer {customer.account_code}",
        details={
            "customer_id": customer.id,
            "customer_account_code": customer.account_code,
            "amount_decimal": str(payload["amount_decimal"]),
            "reason": payload["reason"],
            "created_by": actor,
        },
    )
    db.commit()
    return RedirectResponse(
        url=f"/customers/{customer.id}?saved=1&adjustment_saved=1",
        status_code=303,
    )


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
        "adjustment_reasons": list(ADJUSTMENT_REASONS),
    }


def _empty_override_form() -> dict[str, str]:
    return {
        "product_id": "",
        "unit_price": "",
        "is_active": "on",
    }


def _empty_adjustment_form() -> dict[str, str]:
    return {
        "amount_decimal": "",
        "reason": ADJUSTMENT_REASONS[0][0],
        "note": "",
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


def _parse_adjustment_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    amount_raw = value("amount_decimal")
    reason_raw = value("reason").upper()
    note = value("note")

    validate_no_html_fields(
        {
            "Amount": amount_raw,
            "Reason": reason_raw,
            "Note": note,
        },
        errors,
    )

    amount_decimal = _parse_decimal(amount_raw)
    if not amount_raw:
        errors.append("Adjustment amount is required.")
    elif amount_decimal is None:
        errors.append("Adjustment amount must be a number.")
    elif amount_decimal == 0:
        errors.append("Adjustment amount cannot be 0.")
    elif abs(amount_decimal) > ADJUSTMENT_AMOUNT_MAX:
        errors.append("Adjustment amount is too large.")
    else:
        amount_decimal = amount_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    valid_reason_codes = {code for code, _label in ADJUSTMENT_REASONS}
    if not reason_raw:
        errors.append("Reason is required.")
    elif reason_raw not in valid_reason_codes:
        errors.append("Reason is invalid.")

    if not note:
        errors.append("Note is required for audit purposes.")
    elif len(note) > NOTES_MAX:
        errors.append(f"Note must be {NOTES_MAX} characters or fewer.")

    return {
        "errors": errors,
        "form": {
            "amount_decimal": amount_raw,
            "reason": reason_raw or ADJUSTMENT_REASONS[0][0],
            "note": note,
        },
        "amount_decimal": amount_decimal,
        "reason": reason_raw,
        "note": note,
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


def _customer_adjustments(
    db: Session, customer_id: int, *, limit: int = 10
) -> list[CustomerAdjustment]:
    return (
        db.execute(
            select(CustomerAdjustment)
            .where(CustomerAdjustment.customer_id == customer_id)
            .order_by(CustomerAdjustment.created_at.desc(), CustomerAdjustment.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def _pence_to_money(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return money_decimal(Decimal(value) / Decimal("100"))


def _customer_outstanding_context(
    db: Session, customer: Customer
) -> dict[str, object]:
    outstanding_raw = customer_outstanding_total(db, customer.id)
    outstanding_display, credit_balance = outstanding_display_values(outstanding_raw)
    invoice_total = customer_invoice_outstanding_total(db, customer.id)
    adjustments_total = customer_adjustments_total(db, customer.id)
    credit_limit = _pence_to_money(customer.credit_limit_pence)

    available_credit: Decimal | None = None
    is_available_credit_low = False
    is_over_credit_limit = False
    if credit_limit is not None:
        available_credit = money_decimal(credit_limit - outstanding_raw)
        is_over_credit_limit = outstanding_raw > credit_limit
        if not is_over_credit_limit and credit_limit > 0:
            low_threshold = money_decimal(credit_limit * CREDIT_AVAILABLE_LOW_RATIO)
            is_available_credit_low = available_credit < low_threshold

    if outstanding_raw > 0:
        balance_status = "owes"
        balance_label = "Owes"
        balance_amount = money_decimal(outstanding_raw)
    elif outstanding_raw < 0:
        balance_status = "in_credit"
        balance_label = "In credit"
        balance_amount = money_decimal(abs(outstanding_raw))
    else:
        balance_status = "clear"
        balance_label = "Clear"
        balance_amount = Decimal("0.00")

    return {
        "customer_credit_limit": credit_limit,
        "customer_outstanding_raw": outstanding_raw,
        "customer_outstanding_total": outstanding_display,
        "customer_credit_balance": credit_balance,
        "customer_open_invoices_total": invoice_total,
        "customer_adjustments_total": adjustments_total,
        "customer_available_credit": available_credit,
        "customer_is_available_credit_low": is_available_credit_low,
        "customer_is_over_credit_limit": is_over_credit_limit,
        "customer_balance_status": balance_status,
        "customer_balance_label": balance_label,
        "customer_balance_amount": balance_amount,
    }


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
    adjustment_form: dict | None = None,
    saved: bool = False,
    adjustment_saved: bool = False,
    status_code: int = 400,
) -> HTMLResponse:
    outstanding_context = _customer_outstanding_context(db, customer)
    reason_labels = {
        code: label for code, label in ADJUSTMENT_REASONS
    }
    return templates.TemplateResponse(
        request,
        "customers/edit.html",
        {
            "request": request,
            "errors": errors,
            "saved": saved,
            "adjustment_saved": adjustment_saved,
            "customer": customer,
            "row_version": row_version_token(customer),
            "form": form or _customer_to_form(customer),
            "options": _load_options(db),
            "price_overrides": _customer_price_overrides(db, customer.id),
            "override_form": override_form or _empty_override_form(),
            "customer_adjustments": _customer_adjustments(db, customer.id, limit=10),
            "adjustment_form": adjustment_form or _empty_adjustment_form(),
            "adjustment_form_expanded": adjustment_form is not None,
            "adjustment_reason_labels": reason_labels,
            **outstanding_context,
        },
        status_code=status_code,
    )


def _parse_customer_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    account_code = _normalize_account_code(value("account_code"))
    name = value("name")
    invoice_email = value("invoice_email")
    phone = _normalize_phone(value("phone"))
    address_line1 = value("address_line1")
    address_line2 = value("address_line2")
    city = value("city")
    postcode = _normalize_postcode(value("postcode"))
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
        "account_code": _normalize_account_code(customer.account_code or ""),
        "name": customer.name or "",
        "invoice_email": customer.invoice_email or "",
        "phone": _normalize_phone(customer.phone or ""),
        "address_line1": customer.address_line1 or "",
        "address_line2": customer.address_line2 or "",
        "city": customer.city or "",
        "postcode": _normalize_postcode(customer.postcode or ""),
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
