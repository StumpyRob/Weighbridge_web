from datetime import datetime
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.base import utcnow
from ..models import (
    NominalCode,
    Product,
    ProductGroup,
    TaxRate,
    Unit,
    WasteCode,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = select(Product).order_by(Product.code)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Product.code.ilike(like), Product.description.ilike(like)))
    products = db.execute(query).scalars().all()
    return templates.TemplateResponse(request, 
        "products/list.html",
        {"request": request, "products": products, "q": q or ""},
    )


@router.get("/products/new", response_class=HTMLResponse)
def products_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
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
    unit_error = _validate_unit_selection(db, payload["unit_id"])
    if unit_error:
        payload["errors"].append(unit_error)
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
        group_id=payload["group_id"],
        unit_id=payload["unit_id"],
        tax_rate_id=payload["tax_rate_id"],
        nominal_code_id=payload["nominal_code_id"],
        unit_price=payload["unit_price"],
        account_price=payload["account_price"],
        cash_price=payload["cash_price"],
        min_price=payload["min_price"],
        max_price=payload["max_price"],
        max_qty=payload["max_qty"],
        excess_trigger=payload["excess_trigger"],
        excess_price=payload["excess_price"],
        is_hazardous=payload["is_hazardous"],
        final_disposal=payload["final_disposal"],
        used_on_site=payload["used_on_site"],
        default_waste_code_id=payload["default_waste_code_id"],
    )
    db.add(product)
    db.commit()
    return RedirectResponse(url="/products", status_code=303)


@router.get("/products/units", response_class=HTMLResponse)
def units_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
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
    name = _normalize_unit_name(form.get("name"))
    error = _validate_unit_name(db, name)
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
    unit = Unit(name=name, is_active=True)
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
    form = await request.form()
    name = _normalize_unit_name(form.get("name"))
    error = _validate_unit_name(db, name, current_unit_id=unit.id)
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
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(request, 
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )
    return templates.TemplateResponse(request, 
        "products/edit.html",
        {
            "request": request,
            "errors": [],
            "product": product,
            "form": _product_to_form(product),
            "options": _load_options(db, current_unit_id=product.unit_id),
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
    unit_error = _validate_unit_selection(
        db, payload["unit_id"], current_unit_id=product.unit_id
    )
    if unit_error:
        payload["errors"].append(unit_error)
    if payload["errors"]:
        return templates.TemplateResponse(request, 
            "products/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "product": product,
                "form": payload["form"],
                "options": _load_options(db, current_unit_id=product.unit_id),
            },
            status_code=400,
        )

    product.code = payload["code"]
    product.description = payload["description"]
    product.group_id = payload["group_id"]
    product.unit_id = payload["unit_id"]
    product.tax_rate_id = payload["tax_rate_id"]
    product.nominal_code_id = payload["nominal_code_id"]
    product.unit_price = payload["unit_price"]
    product.account_price = payload["account_price"]
    product.cash_price = payload["cash_price"]
    product.min_price = payload["min_price"]
    product.max_price = payload["max_price"]
    product.max_qty = payload["max_qty"]
    product.excess_trigger = payload["excess_trigger"]
    product.excess_price = payload["excess_price"]
    product.is_hazardous = payload["is_hazardous"]
    product.final_disposal = payload["final_disposal"]
    product.used_on_site = payload["used_on_site"]
    product.default_waste_code_id = payload["default_waste_code_id"]
    product.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url=f"/products/{product.id}", status_code=303)


def _load_options(
    db: Session, current_unit_id: int | None = None
) -> dict[str, list[tuple[str, str]]]:
    groups = db.execute(select(ProductGroup).order_by(ProductGroup.code)).scalars()
    units = list(
        db.execute(
            select(Unit).where(Unit.is_active.is_(True)).order_by(Unit.name)
        ).scalars()
    )
    tax_rates = db.execute(select(TaxRate).order_by(TaxRate.code)).scalars()
    nominal_codes = db.execute(select(NominalCode).order_by(NominalCode.code)).scalars()
    waste_codes = db.execute(select(WasteCode).order_by(WasteCode.code)).scalars()
    unit_options = [(str(row.id), row.name) for row in units]
    if current_unit_id:
        if not any(str(row.id) == str(current_unit_id) for row in units):
            current = db.get(Unit, current_unit_id)
            if current:
                label = (
                    f"{current.name} (inactive)"
                    if not current.is_active
                    else current.name
                )
                unit_options = [(str(current.id), label)] + unit_options
    return {
        "groups": [(str(row.id), row.code) for row in groups],
        "units": unit_options,
        "tax_rates": [(str(row.id), row.code) for row in tax_rates],
        "nominal_codes": [(str(row.id), row.code) for row in nominal_codes],
        "waste_codes": [(str(row.id), row.code) for row in waste_codes],
    }


def _parse_product_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    code = value("code")
    description = value("description")
    if not code:
        errors.append("Code is required.")
    if not description:
        errors.append("Description is required.")
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
            "group_id": value("group_id"),
            "unit_id": value("unit_id"),
            "tax_rate_id": value("tax_rate_id"),
            "nominal_code_id": value("nominal_code_id"),
            "unit_price": unit_price_raw,
            "account_price": value("account_price"),
            "cash_price": value("cash_price"),
            "min_price": value("min_price"),
            "max_price": value("max_price"),
            "max_qty": value("max_qty"),
            "excess_trigger": value("excess_trigger"),
            "excess_price": value("excess_price"),
            "default_waste_code_id": value("default_waste_code_id"),
            "is_hazardous": value("is_hazardous"),
            "final_disposal": value("final_disposal"),
            "used_on_site": value("used_on_site"),
        },
        "code": code,
        "description": description,
        "group_id": _parse_int(value("group_id")),
        "unit_id": _parse_int(value("unit_id")),
        "tax_rate_id": _parse_int(value("tax_rate_id")),
        "nominal_code_id": _parse_int(value("nominal_code_id")),
        "unit_price": unit_price_value if unit_price_value is not None else Decimal("0.00"),
        "account_price": _parse_decimal(value("account_price")),
        "cash_price": _parse_decimal(value("cash_price")),
        "min_price": _parse_decimal(value("min_price")),
        "max_price": _parse_decimal(value("max_price")),
        "max_qty": _parse_float(value("max_qty")),
        "excess_trigger": _parse_float(value("excess_trigger")),
        "excess_price": _parse_decimal(value("excess_price")),
        "default_waste_code_id": _parse_int(value("default_waste_code_id")),
        "is_hazardous": value("is_hazardous") == "on",
        "final_disposal": value("final_disposal") == "on",
        "used_on_site": value("used_on_site") == "on",
    }


def _empty_form() -> dict:
    return {
        "code": "",
        "description": "",
        "group_id": "",
        "unit_id": "",
        "tax_rate_id": "",
        "nominal_code_id": "",
        "unit_price": "",
        "account_price": "",
        "cash_price": "",
        "min_price": "",
        "max_price": "",
        "max_qty": "",
        "excess_trigger": "",
        "excess_price": "",
        "default_waste_code_id": "",
        "is_hazardous": "",
        "final_disposal": "",
        "used_on_site": "",
    }


def _product_to_form(product: Product) -> dict:
    return {
        "code": product.code or "",
        "description": product.description or "",
        "group_id": str(product.group_id or ""),
        "unit_id": str(product.unit_id or ""),
        "tax_rate_id": str(product.tax_rate_id or ""),
        "nominal_code_id": str(product.nominal_code_id or ""),
        "unit_price": _format_decimal(product.unit_price),
        "account_price": _format_decimal(product.account_price),
        "cash_price": _format_decimal(product.cash_price),
        "min_price": _format_decimal(product.min_price),
        "max_price": _format_decimal(product.max_price),
        "max_qty": f"{product.max_qty}" if product.max_qty else "",
        "excess_trigger": f"{product.excess_trigger}" if product.excess_trigger else "",
        "excess_price": _format_decimal(product.excess_price),
        "default_waste_code_id": str(product.default_waste_code_id or ""),
        "is_hazardous": "on" if product.is_hazardous else "",
        "final_disposal": "on" if product.final_disposal else "",
        "used_on_site": "on" if product.used_on_site else "",
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
    db: Session, unit_id: int | None, current_unit_id: int | None = None
) -> str | None:
    if unit_id is None:
        return None
    unit = db.get(Unit, unit_id)
    if not unit:
        return "Unit not found."
    if not unit.is_active and unit_id != current_unit_id:
        return "Unit is inactive."
    return None


def _normalize_unit_name(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", str(raw).strip())


def _validate_unit_name(
    db: Session, name: str, current_unit_id: int | None = None
) -> str | None:
    if not name:
        return "Name is required."
    existing = db.execute(
        select(Unit).where(func.lower(Unit.name) == name.lower())
    ).scalar_one_or_none()
    if existing and existing.id != current_unit_id:
        return "Name already exists."
    return None
