import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.base import utcnow
from ..models import (
    Destination,
    Product,
    TaxRate,
    Unit,
    EwcCode,
)
from ..services.unit_rules import (
    canonical_weight_unit,
    is_allowed_weight_unit,
    normalize_unit_name,
)
from ..security import has_unsafe_markup, validate_no_html_fields
from ..templating import templates

router = APIRouter()
UNIT_TYPES = ("WEIGHT", "COUNT")
SALE_TYPES = ("COUNT", "WEIGHT")
UNIT_NAME_MAX_LEN = 50
SYSTEM_WEIGHT_UNIT_NAME = "tonnes"
SYSTEM_COUNT_UNIT_NAME = "Each"
SYSTEM_TAX_RATE_STANDARD_CODE = "Standard (20%) \u2013 UK VAT"
SYSTEM_TAX_RATE_ZERO_CODE = "Zero (0%) \u2013 UK VAT"
LEGACY_TAX_RATE_STANDARD_CODES = ["Standard (20%)"]
LEGACY_TAX_RATE_ZERO_CODES = ["Zero (0%)"]


@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _backfill_product_units(db)
    _backfill_product_tax_rates(db)
    query = select(Product).order_by(Product.code)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Product.code.ilike(like), Product.description.ilike(like)))
    products = db.execute(query).scalars().all()
    return templates.TemplateResponse(request, 
        "products/list.html",
        {
            "request": request,
            "products": products,
            "q": q or "",
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
def products_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _ensure_system_units(db)
    _ensure_system_tax_rates(db)
    return templates.TemplateResponse(request, 
        "products/new.html",
        {
            "request": request,
            "errors": [],
            "form": _empty_form(),
        "options": _load_options(db),
    },
)


@router.post("/products/new", response_class=HTMLResponse)
async def products_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_product_form(form)
    payload["errors"].extend(_apply_sale_type_unit_selection(db, payload))
    if not payload["errors"]:
        tax_rate_error = _validate_tax_rate_selection(
            db, payload.get("tax_rate_id"), required=True
        )
        if tax_rate_error:
            payload["errors"].append(tax_rate_error)
    if not payload["errors"] and payload["unit_id"]:
        unit = db.get(Unit, payload["unit_id"])
        if unit and unit.unit_type == "WEIGHT" and not is_allowed_weight_unit(unit.name):
            payload["errors"].append("Selected WEIGHT unit is not supported.")
    if payload["errors"]:
        return templates.TemplateResponse(request, 
            "products/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )

    product = Product(
        code=payload["code"],
        description=payload["description"],
        unit_id=payload["unit_id"],
        tax_rate_id=payload["tax_rate_id"],
        unit_price=payload["unit_price"],
        is_hazardous=payload["is_hazardous"],
        ewc_code_id=payload["ewc_code_id"],
        default_destination_id=payload["default_destination_id"],
    )
    db.add(product)
    db.commit()
    return RedirectResponse(url="/products?saved=1", status_code=303)


@router.get("/products/units", response_class=HTMLResponse)
def units_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    error = None
    error_code = request.query_params.get("error")
    if error_code == "system":
        error = "System units (kg/tonnes) are read-only."
    query = select(Unit)
    if resolved_hide:
        query = query.where(Unit.is_active.is_(True))
    if q:
        like = f"%{q.lower()}%"
        query = query.where(func.lower(Unit.name).like(like))
    items = db.execute(query.order_by(Unit.name.asc())).scalars().all()
    return templates.TemplateResponse(request, 
        "lookups/list.html",
        {
            "request": request,
            "entity_plural": "Units",
            "entity_singular": "Unit",
            "base_path": "/products/units",
            "items": items,
            "q": q or "",
            "hide_inactive": bool(resolved_hide),
            "saved": request.query_params.get("saved") == "1",
            "show_tabs": False,
            "error": error,
        },
    )


@router.get("/products/units/new", response_class=HTMLResponse)
def units_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Units",
            "entity_singular": "Unit",
            "base_path": "/products/units",
            "mode": "new",
            "item": None,
            "prefill_name": "",
            "error": None,
        },
    )


@router.post("/products/units/new", response_class=HTMLResponse)
async def units_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    unit_type = _normalize_unit_type(form.get("unit_type"))
    name = _normalize_unit_name_for_type(form.get("name"), unit_type)
    error = _validate_unit_name(db, name)
    if not error and unit_type not in UNIT_TYPES:
        error = "Unit type must be WEIGHT or COUNT."
    if not error and unit_type == "WEIGHT":
        error = "Weight units are system-defined and cannot be created."
    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Units",
                "entity_singular": "Unit",
                "base_path": "/products/units",
                "mode": "new",
                "item": None,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )
    unit = Unit(name=name, unit_type=unit_type, is_active=True)
    db.add(unit)
    db.commit()
    return RedirectResponse(url="/products/units?saved=1", status_code=303)


@router.get("/products/units/{unit_id}/edit", response_class=HTMLResponse)
def units_edit(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Unit",
                "entity_id": unit_id,
                "base_path": "/products/units",
            },
            status_code=404,
        )
    if unit.unit_type == "WEIGHT":
        return RedirectResponse(url="/products/units?error=system", status_code=303)
    return templates.TemplateResponse(request, 
        "lookups/form.html",
        {
            "request": request,
            "entity_plural": "Units",
            "entity_singular": "Unit",
            "base_path": "/products/units",
            "mode": "edit",
            "item": unit,
            "prefill_name": None,
            "error": None,
        },
    )


@router.post("/products/units/{unit_id}/edit", response_class=HTMLResponse)
async def units_update(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Unit",
                "entity_id": unit_id,
                "base_path": "/products/units",
            },
            status_code=404,
        )
    if unit.unit_type == "WEIGHT":
        return RedirectResponse(url="/products/units?error=system", status_code=303)
    form = await request.form()
    unit_type = _normalize_unit_type(form.get("unit_type"))
    name = _normalize_unit_name_for_type(form.get("name"), unit_type)
    error = _validate_unit_name(db, name, current_unit_id=unit.id)
    if not error and unit_type not in UNIT_TYPES:
        error = "Unit type must be WEIGHT or COUNT."
    if not error and unit_type == "WEIGHT":
        error = "Weight units are system-defined and cannot be created."
    if error:
        return templates.TemplateResponse(request, 
            "lookups/form.html",
            {
                "request": request,
                "entity_plural": "Units",
                "entity_singular": "Unit",
                "base_path": "/products/units",
                "mode": "edit",
                "item": unit,
                "prefill_name": name,
                "error": error,
            },
            status_code=400,
        )
    unit.name = name
    unit.unit_type = unit_type
    unit.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products/units?saved=1", status_code=303)


@router.post("/products/units/{unit_id}/deactivate", response_class=HTMLResponse)
def units_deactivate(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Unit",
                "entity_id": unit_id,
                "base_path": "/products/units",
            },
            status_code=404,
        )
    if unit.unit_type == "WEIGHT":
        return RedirectResponse(url="/products/units?error=system", status_code=303)
    unit.is_active = False
    unit.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products/units?saved=1", status_code=303)


@router.post("/products/units/{unit_id}/reactivate", response_class=HTMLResponse)
def units_reactivate(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(request, 
            "lookups/not_found.html",
            {
                "request": request,
                "entity": "Unit",
                "entity_id": unit_id,
                "base_path": "/products/units",
            },
            status_code=404,
        )
    if unit.unit_type == "WEIGHT":
        return RedirectResponse(url="/products/units?error=system", status_code=303)
    unit.is_active = True
    unit.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products/units?saved=1", status_code=303)


def _resolve_hide_inactive(request: Request, hide_inactive: int | None) -> int:
    if hide_inactive is not None:
        return 1 if hide_inactive else 0
    legacy_show = request.query_params.get("show_inactive")
    if legacy_show is None:
        return 1
    return 0 if str(legacy_show).lower() in {"1", "true", "yes", "on"} else 1


@router.get("/products/{product_id:int}", response_class=HTMLResponse)
def products_edit(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    _ensure_system_units(db)
    _ensure_system_tax_rates(db)
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(request, 
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )
    _backfill_product_units(db)
    _backfill_product_tax_rates(db)
    return templates.TemplateResponse(request, 
        "products/edit.html",
        {
            "request": request,
            "errors": [],
            "product": product,
            "form": _product_to_form(product),
            "options": _load_options(
                db,
                current_unit_id=product.unit_id,
                current_ewc_code_id=product.ewc_code_id,
                current_default_destination_id=product.default_destination_id,
            ),
        },
    )


@router.post("/products/{product_id:int}", response_class=HTMLResponse)
async def products_update(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(request, 
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_product_form(form)
    payload["errors"].extend(
        _apply_sale_type_unit_selection(
            db, payload, current_unit_id=product.unit_id
        )
    )
    if not payload["errors"]:
        tax_rate_error = _validate_tax_rate_selection(
            db,
            payload.get("tax_rate_id"),
            current_tax_rate_id=product.tax_rate_id,
            required=True,
        )
        if tax_rate_error:
            payload["errors"].append(tax_rate_error)
    if not payload["errors"] and payload["unit_id"]:
        unit = db.get(Unit, payload["unit_id"])
        if unit and unit.unit_type == "WEIGHT" and not is_allowed_weight_unit(unit.name):
            payload["errors"].append("Selected WEIGHT unit is not supported.")
    if payload["errors"]:
        return templates.TemplateResponse(request, 
            "products/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "product": product,
                "form": payload["form"],
                "options": _load_options(
                    db,
                    current_unit_id=product.unit_id,
                    current_ewc_code_id=product.ewc_code_id,
                    current_default_destination_id=product.default_destination_id,
                ),
            },
            status_code=400,
        )

    product.code = payload["code"]
    product.description = payload["description"]
    product.unit_id = payload["unit_id"]
    product.tax_rate_id = payload["tax_rate_id"]
    product.unit_price = payload["unit_price"]
    product.is_hazardous = payload["is_hazardous"]
    product.ewc_code_id = payload["ewc_code_id"]
    product.default_destination_id = payload["default_destination_id"]
    product.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products?saved=1", status_code=303)


def _load_options(
    db: Session,
    current_unit_id: int | None = None,
    current_ewc_code_id: int | None = None,
    current_default_destination_id: int | None = None,
) -> dict[str, list[tuple[str, str]]]:
    units = list(
        db.execute(
            select(Unit)
            .where(Unit.is_active.is_(True), Unit.unit_type == "COUNT")
            .order_by(Unit.name)
        ).scalars()
    )
    tax_rates = db.execute(select(TaxRate).order_by(TaxRate.code)).scalars()
    ewc_codes = list(
        db.execute(
            select(EwcCode).where(EwcCode.active.is_(True)).order_by(EwcCode.code_6)
        ).scalars()
    )
    destinations = list(
        db.execute(
            select(Destination)
            .where(Destination.is_active.is_(True))
            .order_by(Destination.name)
        ).scalars()
    )
    unit_options = [(str(row.id), row.name) for row in units]
    if current_unit_id:
        if not any(str(row.id) == str(current_unit_id) for row in units):
            current = db.get(Unit, current_unit_id)
            if current and current.unit_type == "COUNT":
                label = (
                    f"{current.name} (inactive)"
                    if not current.is_active
                    else current.name
                )
                unit_options = [(str(current.id), label)] + unit_options
    ewc_options = [
        (
            str(row.id),
            f"{row.code_display} - {row.description}",
            row.hazardous,
            row.code_6,
        )
        for row in ewc_codes
    ]
    if current_ewc_code_id:
        if not any(str(row.id) == str(current_ewc_code_id) for row in ewc_codes):
            current = db.get(EwcCode, current_ewc_code_id)
            if current:
                label = f"{current.code_display} - {current.description}"
                if not current.active:
                    label = f"{label} (inactive)"
                ewc_options = [
                    (str(current.id), label, current.hazardous, current.code_6)
                ] + ewc_options
    destination_options = [(str(row.id), row.name) for row in destinations]
    if current_default_destination_id:
        if not any(
            str(row.id) == str(current_default_destination_id) for row in destinations
        ):
            current = db.get(Destination, current_default_destination_id)
            if current:
                label = (
                    f"{current.name} (inactive)"
                    if not current.is_active
                    else current.name
                )
                destination_options = [(str(current.id), label)] + destination_options
    return {
        "units": unit_options,
        "tax_rates": [(str(row.id), row.code) for row in tax_rates],
        "ewc_codes": ewc_options,
        "destinations": destination_options,
    }


def _parse_product_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    code = value("code")
    description = value("description")
    sale_type = _normalize_sale_type(value("sale_type"))
    tax_rate_raw = value("tax_rate_id")
    tax_rate_id = _parse_int(tax_rate_raw)

    validate_no_html_fields(
        {
            "Code": code,
            "Description": description,
        },
        errors,
    )

    if not code:
        errors.append("Code is required.")
    if not description:
        errors.append("Description is required.")
    if not sale_type:
        errors.append("Sale type is required.")
    elif sale_type not in SALE_TYPES:
        errors.append("Sale type must be WEIGHT or COUNT.")
    if not tax_rate_raw:
        errors.append("Tax rate is required.")
    unit_price_raw = value("unit_price")
    if not unit_price_raw:
        errors.append("Unit price is required.")
    unit_price_value = _parse_decimal(unit_price_raw)
    if unit_price_raw and unit_price_value is None:
        errors.append("Unit price must be a number.")
    if unit_price_value is not None and unit_price_value < 0:
        errors.append("Unit price must be 0 or greater.")

    return {
        "errors": errors,
        "form": {
            "code": code,
            "description": description,
            "sale_type": sale_type,
            "unit_id": value("unit_id"),
            "tax_rate_id": value("tax_rate_id"),
            "unit_price": unit_price_raw,
            "ewc_code_id": value("ewc_code_id"),
            "ewc_code_label": value("ewc_code_label"),
            "default_destination_id": value("default_destination_id"),
            "is_hazardous": value("is_hazardous"),
        },
        "sale_type": sale_type,
        "code": code,
        "description": description,
        "unit_id": _parse_int(value("unit_id")),
        "tax_rate_id": tax_rate_id,
        "unit_price": unit_price_value if unit_price_value is not None else Decimal("0.00"),
        "ewc_code_id": _parse_int(value("ewc_code_id")),
        "ewc_code_label": value("ewc_code_label"),
        "default_destination_id": _parse_int(value("default_destination_id")),
        "is_hazardous": value("is_hazardous") == "on",
    }


def _empty_form() -> dict:
    return {
        "code": "",
        "description": "",
        "sale_type": "",
        "unit_id": "",
        "tax_rate_id": "",
        "unit_price": "",
        "ewc_code_id": "",
        "ewc_code_label": "",
        "default_destination_id": "",
        "is_hazardous": "",
    }


def _product_to_form(product: Product) -> dict:
    unit_type = product.unit.unit_type if product.unit else None
    sale_type = "WEIGHT" if unit_type == "WEIGHT" else "COUNT"
    return {
        "code": product.code or "",
        "description": product.description or "",
        "sale_type": sale_type,
        "unit_id": str(product.unit_id or ""),
        "tax_rate_id": str(product.tax_rate_id or ""),
        "unit_price": _format_decimal(product.unit_price),
        "ewc_code_id": str(product.ewc_code_id or ""),
        "ewc_code_label": (
            f"{product.ewc_code.code_display} - {product.ewc_code.description}"
            if product.ewc_code
            else ""
        ),
        "default_destination_id": str(product.default_destination_id or ""),
        "is_hazardous": "on" if product.is_hazardous else "",
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


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def _validate_unit_selection(
    db: Session,
    unit_id: int | None,
    current_unit_id: int | None = None,
    *,
    required: bool = False,
) -> str | None:
    if unit_id is None:
        return "Unit is required." if required else None
    unit = db.get(Unit, unit_id)
    if not unit:
        return "Unit not found."
    if not unit.is_active and unit_id != current_unit_id:
        return "Unit is inactive."
    return None


def _validate_tax_rate_selection(
    db: Session,
    tax_rate_id: int | None,
    current_tax_rate_id: int | None = None,
    *,
    required: bool = False,
) -> str | None:
    if tax_rate_id is None:
        return "Tax rate is required." if required else None
    tax_rate = db.get(TaxRate, tax_rate_id)
    if not tax_rate:
        return "Tax rate not found."
    if not tax_rate.is_active and tax_rate_id != current_tax_rate_id:
        return "Tax rate is inactive."
    return None


def _find_unit_by_names(db: Session, names: list[str]) -> Unit | None:
    normalized = [normalize_unit_name(name) for name in names]
    return (
        db.execute(select(Unit).where(func.lower(Unit.name).in_(normalized)))
        .scalar_one_or_none()
    )


def _ensure_system_units(db: Session) -> dict[str, Unit]:
    now = utcnow()
    updated = False

    def get_or_create(name: str, unit_type: str, aliases: list[str] | None = None) -> Unit:
        nonlocal updated
        changed = False
        search_names = [name] + (aliases or [])
        unit = _find_unit_by_names(db, search_names)
        if unit:
            if unit.name != name:
                unit.name = name
                changed = True
            if unit.unit_type != unit_type:
                unit.unit_type = unit_type
                changed = True
            if not unit.is_active:
                unit.is_active = True
                changed = True
            if changed:
                unit.updated_at = now
                updated = True
            return unit
        unit = Unit(name=name, unit_type=unit_type, is_active=True, updated_at=now)
        db.add(unit)
        updated = True
        return unit

    kg = get_or_create("kg", "WEIGHT")
    tonnes = get_or_create("tonnes", "WEIGHT", aliases=["tonne"])
    each = get_or_create(SYSTEM_COUNT_UNIT_NAME, "COUNT")

    if updated:
        db.commit()
        db.refresh(kg)
        db.refresh(tonnes)
        db.refresh(each)

    return {"kg": kg, "tonnes": tonnes, "each": each}


def _ensure_system_tax_rates(db: Session) -> dict[str, TaxRate]:
    now = utcnow()
    updated = False

    def get_or_create(
        code: str,
        *,
        rate_percent: Decimal,
        description: str,
        aliases: list[str] | None = None,
    ) -> TaxRate:
        nonlocal updated

        search_codes = [code] + (aliases or [])
        matches = list(
            db.execute(
                select(TaxRate).where(
                    func.lower(TaxRate.code).in_([item.lower() for item in search_codes])
                )
            ).scalars()
        )
        existing = next(
            (
                row
                for row in matches
                if row.code.strip().lower() == code.strip().lower()
            ),
            None,
        ) or (matches[0] if matches else None)

        if existing:
            for other in matches:
                if other.id == existing.id:
                    continue
                impacted_products = list(
                    db.execute(
                        select(Product).where(Product.tax_rate_id == other.id)
                    ).scalars()
                )
                for product in impacted_products:
                    product.tax_rate_id = existing.id
                    product.updated_at = now
                if impacted_products:
                    updated = True
                db.delete(other)
                updated = True

            changed = False
            if existing.code != code:
                existing.code = code
                changed = True
            existing_rate = (
                Decimal(str(existing.rate_percent))
                if existing.rate_percent is not None
                else None
            )
            if existing_rate != rate_percent:
                existing.rate_percent = rate_percent
                changed = True
            if existing.description != description:
                existing.description = description
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if changed:
                existing.updated_at = now
                updated = True
            return existing

        tax_rate = TaxRate(
            code=code,
            description=description,
            rate_percent=rate_percent,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(tax_rate)
        updated = True
        return tax_rate

    standard = get_or_create(
        SYSTEM_TAX_RATE_STANDARD_CODE,
        rate_percent=Decimal("0.20"),
        description="UK VAT standard rate",
        aliases=LEGACY_TAX_RATE_STANDARD_CODES,
    )
    zero = get_or_create(
        SYSTEM_TAX_RATE_ZERO_CODE,
        rate_percent=Decimal("0.00"),
        description="UK VAT zero rate",
        aliases=LEGACY_TAX_RATE_ZERO_CODES,
    )

    if updated:
        db.commit()
        db.refresh(standard)
        db.refresh(zero)

    return {"standard": standard, "zero": zero}


def _apply_sale_type_unit_selection(
    db: Session, payload: dict, current_unit_id: int | None = None
) -> list[str]:
    errors: list[str] = []
    sale_type = payload.get("sale_type") or ""
    if not sale_type or sale_type not in SALE_TYPES:
        return errors

    system_units = _ensure_system_units(db)
    if sale_type == "WEIGHT":
        payload["unit_id"] = system_units["tonnes"].id
        form_data = payload.get("form")
        if isinstance(form_data, dict):
            form_data["unit_id"] = str(payload["unit_id"])
        return errors

    unit_error = _validate_unit_selection(
        db, payload.get("unit_id"), current_unit_id=current_unit_id, required=True
    )
    if unit_error:
        errors.append(unit_error)
        return errors

    unit = db.get(Unit, payload["unit_id"])
    if unit and unit.unit_type != "COUNT":
        errors.append("Selected unit must be a COUNT unit.")
    return errors


def _backfill_product_units(db: Session) -> None:
    system_units = _ensure_system_units(db)
    weight_unit_id = system_units["tonnes"].id
    count_unit_id = system_units["each"].id
    updated = False

    missing_units = db.execute(
        select(Product).where(Product.unit_id.is_(None))
    ).scalars().all()
    for product in missing_units:
        product.unit_id = count_unit_id
        product.updated_at = utcnow()
        updated = True

    weight_products = db.execute(
        select(Product, Unit)
        .join(Unit, Product.unit_id == Unit.id)
        .where(Unit.unit_type == "WEIGHT")
    ).all()
    for product, _unit in weight_products:
        if product.unit_id != weight_unit_id:
            product.unit_id = weight_unit_id
            product.updated_at = utcnow()
            updated = True

    if updated:
        db.commit()


def _backfill_product_tax_rates(db: Session) -> None:
    system_tax_rates = _ensure_system_tax_rates(db)
    standard_rate = system_tax_rates["standard"]

    updated = False
    missing_tax_rates = db.execute(
        select(Product).where(Product.tax_rate_id.is_(None))
    ).scalars().all()
    for product in missing_tax_rates:
        product.tax_rate_id = standard_rate.id
        product.updated_at = utcnow()
        updated = True

    if updated:
        db.commit()


def _normalize_unit_name(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", str(raw).strip())


def _normalize_unit_name_for_type(raw: str | None, unit_type: str) -> str:
    name = _normalize_unit_name(raw)
    if unit_type == "WEIGHT":
        canonical = canonical_weight_unit(name)
        return canonical or name
    if name:
        return name.title()
    return name


def _normalize_unit_type(raw: str | None) -> str:
    value = str(raw or "").strip().upper()
    return value or "COUNT"


def _normalize_sale_type(raw: str | None) -> str:
    return str(raw or "").strip().upper()


def _validate_unit_name(
    db: Session, name: str, current_unit_id: int | None = None
) -> str | None:
    if not name:
        return "Name is required."
    if has_unsafe_markup(name):
        return "HTML is not allowed."
    if len(name) > UNIT_NAME_MAX_LEN:
        return f"Name must be {UNIT_NAME_MAX_LEN} characters or fewer."
    existing = db.execute(
        select(Unit).where(func.lower(Unit.name) == name.lower())
    ).scalar_one_or_none()
    if existing and existing.id != current_unit_id:
        return "Name already exists."
    return None
