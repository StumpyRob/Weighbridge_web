from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
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
    return templates.TemplateResponse(
        "products/list.html",
        {"request": request, "products": products, "q": q or ""},
    )


@router.get("/products/new", response_class=HTMLResponse)
def products_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        "products/new.html",
        {"request": request, "errors": [], "form": _empty_form(), "options": _load_options(db)},
    )


@router.post("/products/new", response_class=HTMLResponse)
async def products_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_product_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
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


@router.get("/products/{product_id}", response_class=HTMLResponse)
def products_edit(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        "products/edit.html",
        {
            "request": request,
            "errors": [],
            "product": product,
            "form": _product_to_form(product),
            "options": _load_options(db),
        },
    )


@router.post("/products/{product_id}", response_class=HTMLResponse)
async def products_update(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )

    form = await request.form()
    payload = _parse_product_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "products/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "product": product,
                "form": payload["form"],
                "options": _load_options(db),
            },
            status_code=400,
        )

    product.code = payload["code"]
    product.description = payload["description"]
    product.group_id = payload["group_id"]
    product.unit_id = payload["unit_id"]
    product.tax_rate_id = payload["tax_rate_id"]
    product.nominal_code_id = payload["nominal_code_id"]
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
    product.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/products/{product.id}", status_code=303)


def _load_options(db: Session) -> dict[str, list[tuple[str, str]]]:
    groups = db.execute(select(ProductGroup).order_by(ProductGroup.code)).scalars()
    units = db.execute(select(Unit).order_by(Unit.code)).scalars()
    tax_rates = db.execute(select(TaxRate).order_by(TaxRate.code)).scalars()
    nominal_codes = db.execute(select(NominalCode).order_by(NominalCode.code)).scalars()
    waste_codes = db.execute(select(WasteCode).order_by(WasteCode.code)).scalars()
    return {
        "groups": [(str(row.id), row.code) for row in groups],
        "units": [(str(row.id), row.code) for row in units],
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

    return {
        "errors": errors,
        "form": {
            "code": code,
            "description": description,
            "group_id": value("group_id"),
            "unit_id": value("unit_id"),
            "tax_rate_id": value("tax_rate_id"),
            "nominal_code_id": value("nominal_code_id"),
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
        "account_price": _parse_float(value("account_price")),
        "cash_price": _parse_float(value("cash_price")),
        "min_price": _parse_float(value("min_price")),
        "max_price": _parse_float(value("max_price")),
        "max_qty": _parse_float(value("max_qty")),
        "excess_trigger": _parse_float(value("excess_trigger")),
        "excess_price": _parse_float(value("excess_price")),
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
        "account_price": f"{product.account_price}" if product.account_price else "",
        "cash_price": f"{product.cash_price}" if product.cash_price else "",
        "min_price": f"{product.min_price}" if product.min_price else "",
        "max_price": f"{product.max_price}" if product.max_price else "",
        "max_qty": f"{product.max_qty}" if product.max_qty else "",
        "excess_trigger": f"{product.excess_trigger}" if product.excess_trigger else "",
        "excess_price": f"{product.excess_price}" if product.excess_price else "",
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
