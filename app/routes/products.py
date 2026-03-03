import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..audit import log as audit_log
from ..constants import CODE_MAX, DESC_MAX, NAME_MAX, NOMINAL_CODE_MAX
from ..db import get_db
from ..models.base import utcnow
from ..models import (
    CustomerProductPrice,
    Destination,
    NominalCode,
    Product,
    ProductGroup,
    TaxRate,
    Ticket,
    Unit,
    EwcCode,
)
from ..services.pricing import product_effective_nominal_code
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
UNIT_NAME_MAX_LEN = NAME_MAX
SYSTEM_WEIGHT_UNIT_NAME = "Tonnes"
SYSTEM_COUNT_UNIT_NAME = "Each"
SYSTEM_TAX_RATE_STANDARD_CODE = "Standard (20%) \u2013 UK VAT"
SYSTEM_TAX_RATE_ZERO_CODE = "Zero (0%)"
LEGACY_TAX_RATE_STANDARD_CODES = ["Standard (20%)"]
LEGACY_TAX_RATE_ZERO_CODES = ["Zero (0%) \u2013 UK VAT"]
PRODUCT_SEARCH_MAX_LEN = 100
PRODUCT_GROUP_NAME_MAX_LEN = NAME_MAX
PRODUCT_GROUP_DESCRIPTION_MAX_LEN = DESC_MAX
NOMINAL_CODE_MAX_LEN = NOMINAL_CODE_MAX


@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _backfill_product_units(db)
    _backfill_product_tax_rates(db)
    search_query = _normalize_search_query(q)
    query = (
        select(Product)
        .options(
            selectinload(Product.unit),
            selectinload(Product.product_group),
            selectinload(Product.ewc_code),
            selectinload(Product.tax_rate),
        )
        .order_by(Product.code)
    )
    if search_query:
        like = _contains_like_pattern(search_query)
        query = query.outerjoin(EwcCode, Product.ewc_code_id == EwcCode.id).where(
            or_(
                Product.code.ilike(like, escape="\\"),
                Product.description.ilike(like, escape="\\"),
                EwcCode.code_display.ilike(like, escape="\\"),
                EwcCode.code_6.ilike(like, escape="\\"),
                EwcCode.description.ilike(like, escape="\\"),
            )
        )
    products = db.execute(query).scalars().all()
    error = None
    error_code = request.query_params.get("error")
    if error_code == "in_use":
        error = "Cannot delete: in use by tickets."
    return templates.TemplateResponse(request, 
        "products/list.html",
        {
            "request": request,
            "products": products,
            "q": search_query,
            "saved": request.query_params.get("saved") == "1",
            "error": error,
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
def products_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    options = _load_options(db)
    errors: list[str] = []
    if not options.get("units"):
        errors.append("System not initialized: missing required lookups (units).")
    if not options.get("tax_rates"):
        errors.append("System not initialized: missing required lookups (tax rates).")
    return templates.TemplateResponse(
        request,
        "products/new.html",
        {
            "request": request,
            "errors": errors,
            "form": _empty_form(),
            "options": options,
        },
        status_code=503 if errors else 200,
    )


@router.post("/products/new", response_class=HTMLResponse)
async def products_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_product_form(form)
    duplicate_error = _validate_product_code_unique(db, payload.get("code"))
    if duplicate_error:
        payload["errors"].append(duplicate_error)
    payload["errors"].extend(_apply_sale_type_unit_selection(db, payload))
    if not payload["errors"] and payload["unit_id"]:
        unit = db.get(Unit, payload["unit_id"])
        if unit and unit.unit_type == "WEIGHT" and not is_allowed_weight_unit(unit.name):
            payload["errors"].append("Selected WEIGHT unit is not supported.")
    if not payload["errors"]:
        group_error = _validate_product_group_selection(
            db, payload.get("group_id"), required=False
        )
        if group_error:
            payload["errors"].append(group_error)
    if payload["errors"]:
        _hydrate_effective_nominal_code_form(db, payload["form"])
        return templates.TemplateResponse(request, 
            "products/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db, current_group_id=payload.get("group_id")),
            },
            status_code=400,
        )

    product = Product(
        code=payload["code"],
        description=payload["description"],
        sales_only=payload["sales_only"],
        group_id=payload["group_id"],
        unit_id=payload["unit_id"],
        tax_rate_id=payload["tax_rate_id"],
        nominal_code=payload["nominal_code"],
        unit_price=payload["unit_price"],
        account_price=payload["account_price"],
        cash_price=payload["cash_price"],
        min_price=payload["min_price"],
        max_price=payload["max_price"],
        max_qty=payload["max_qty"],
        excess_trigger=payload["excess_trigger"],
        excess_price=payload["excess_price"],
        is_hazardous=payload["is_hazardous"],
        final_disposal_wip=payload["final_disposal_wip"],
        used_on_site_wip=payload["used_on_site_wip"],
        ewc_code_id=payload["ewc_code_id"],
        default_destination_id=payload["default_destination_id"],
    )
    db.add(product)
    try:
        db.flush()
        audit_log(
            db,
            request,
            action="CREATE",
            entity_type="product",
            entity_id=product.id,
            summary=f"Created product {product.code}",
            details={
                "code": product.code,
                "description": product.description,
            },
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Product code already exists.")
        _hydrate_effective_nominal_code_form(db, payload["form"])
        return templates.TemplateResponse(
            request,
            "products/new.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "options": _load_options(db, current_group_id=payload.get("group_id")),
            },
            status_code=400,
        )
    return RedirectResponse(url="/products?saved=1", status_code=303)


@router.get("/products/groups", response_class=HTMLResponse)
def product_groups_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = Query(None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    error = None
    error_code = request.query_params.get("error")
    if error_code == "in_use":
        error = "Cannot delete: in use by products."
    elif error_code:
        error = request.query_params.get("error") or ""

    query = select(ProductGroup)
    if resolved_hide:
        query = query.where(ProductGroup.is_active.is_(True))
    if q:
        like = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(ProductGroup.name).like(like),
                func.lower(ProductGroup.code).like(like),
                func.lower(func.coalesce(ProductGroup.description, "")).like(like),
                func.lower(func.coalesce(ProductGroup.nominal_code_default, "")).like(
                    like
                ),
            )
        )
    groups = db.execute(query.order_by(ProductGroup.name.asc())).scalars().all()
    group_ids = [int(group.id) for group in groups]
    product_counts_by_group: dict[int, int] = {}
    if group_ids:
        count_rows = db.execute(
            select(Product.group_id, func.count(Product.id))
            .where(Product.group_id.in_(group_ids))
            .group_by(Product.group_id)
        ).all()
        product_counts_by_group = {
            int(group_id): int(count)
            for group_id, count in count_rows
            if group_id is not None
        }
    return templates.TemplateResponse(
        request,
        "products/groups_list.html",
        {
            "request": request,
            "groups": groups,
            "product_counts_by_group": product_counts_by_group,
            "q": q or "",
            "hide_inactive": bool(resolved_hide),
            "saved": request.query_params.get("saved") == "1",
            "error": error or "",
        },
    )


@router.get("/products/groups/new", response_class=HTMLResponse)
def product_groups_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "products/group_form.html",
        {
            "request": request,
            "errors": [],
            "mode": "new",
            "group": None,
            "form": _empty_product_group_form(),
        },
    )


@router.post("/products/groups/new", response_class=HTMLResponse)
async def product_groups_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    form = await request.form()
    payload = _parse_product_group_form(form)
    duplicate_error = _validate_product_group_name_unique(
        db,
        payload.get("name"),
    )
    if duplicate_error:
        payload["errors"].append(duplicate_error)
    if payload["errors"]:
        return templates.TemplateResponse(
            request,
            "products/group_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "mode": "new",
                "group": None,
                "form": payload["form"],
            },
            status_code=400,
        )

    group = ProductGroup(
        code=_build_product_group_code(db, payload["name"]),
        name=payload["name"],
        description=payload["description"],
        nominal_code_default=payload["nominal_code_default"],
        is_active=True,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Product group name already exists.")
        return templates.TemplateResponse(
            request,
            "products/group_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "mode": "new",
                "group": None,
                "form": payload["form"],
            },
            status_code=400,
        )
    return RedirectResponse(url="/products/groups?saved=1", status_code=303)


@router.get("/products/groups/{group_id:int}/edit", response_class=HTMLResponse)
def product_groups_edit(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    group = db.get(ProductGroup, group_id)
    if not group:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": group_id},
            status_code=404,
        )
    products_in_group = list(
        db.execute(
            select(Product)
            .where(Product.group_id == group.id)
            .order_by(Product.code.asc(), Product.description.asc())
        ).scalars()
    )
    return templates.TemplateResponse(
        request,
        "products/group_form.html",
        {
            "request": request,
            "errors": [],
            "mode": "edit",
            "group": group,
            "form": _product_group_to_form(group),
            "products_in_group": products_in_group,
        },
    )


@router.post("/products/groups/{group_id:int}/edit", response_class=HTMLResponse)
async def product_groups_update(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    group = db.get(ProductGroup, group_id)
    if not group:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": group_id},
            status_code=404,
        )
    form = await request.form()
    payload = _parse_product_group_form(form)
    duplicate_error = _validate_product_group_name_unique(
        db,
        payload.get("name"),
        current_group_id=group.id,
    )
    if duplicate_error:
        payload["errors"].append(duplicate_error)
    if payload["errors"]:
        return templates.TemplateResponse(
            request,
            "products/group_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "mode": "edit",
                "group": group,
                "form": payload["form"],
            },
            status_code=400,
        )

    group.name = payload["name"]
    group.description = payload["description"]
    group.nominal_code_default = payload["nominal_code_default"]
    group.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Product group name already exists.")
        return templates.TemplateResponse(
            request,
            "products/group_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "mode": "edit",
                "group": group,
                "form": payload["form"],
            },
            status_code=400,
        )
    return RedirectResponse(url="/products/groups?saved=1", status_code=303)


@router.post("/products/groups/{group_id:int}/deactivate", response_class=HTMLResponse)
def product_groups_deactivate(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    group = db.get(ProductGroup, group_id)
    if not group:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": group_id},
            status_code=404,
        )
    in_use = db.execute(
        select(func.count(Product.id)).where(Product.group_id == group.id)
    ).scalar()
    if in_use and in_use > 0:
        return RedirectResponse(
            url="/products/groups?error=Cannot+deactivate:+in+use+by+products.",
            status_code=303,
        )

    group.is_active = False
    group.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products/groups?saved=1", status_code=303)


@router.post("/products/groups/{group_id:int}/reactivate", response_class=HTMLResponse)
def product_groups_reactivate(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    group = db.get(ProductGroup, group_id)
    if not group:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": group_id},
            status_code=404,
        )
    group.is_active = True
    group.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/products/groups?saved=1", status_code=303)


@router.post("/products/groups/{group_id:int}/delete", response_class=HTMLResponse)
def product_groups_delete(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    group = db.get(ProductGroup, group_id)
    if not group:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": group_id},
            status_code=404,
        )

    in_use = db.execute(
        select(func.count(Product.id)).where(Product.group_id == group.id)
    ).scalar_one()
    if in_use:
        return RedirectResponse(url="/products/groups?error=in_use", status_code=303)

    db.delete(group)
    db.commit()
    return RedirectResponse(url="/products/groups?saved=1", status_code=303)


@router.get("/products/units", response_class=HTMLResponse)
def units_list(
    request: Request,
    q: str | None = None,
    hide_inactive: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _ensure_system_units(db)
    resolved_hide = _resolve_hide_inactive(request, hide_inactive)
    error = None
    error_code = request.query_params.get("error")
    if error_code == "system":
        error = "System units (KG/Tonnes) are read-only."
    elif error_code == "in_use":
        error = "Unit is in use by one or more products and cannot be deleted."
    query = select(Unit)
    if resolved_hide:
        query = query.where(Unit.is_active.is_(True))
    if q:
        like = f"%{q.lower()}%"
        query = query.where(func.lower(Unit.name).like(like))
    items = sorted(list(db.execute(query).scalars()), key=_unit_list_order_key)
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
        error = "Weight units are system-defined (KG/Tonnes) and cannot be created."
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
        error = "Weight units are system-defined (KG/Tonnes) and cannot be created."
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


@router.post("/products/units/{unit_id}/delete", response_class=HTMLResponse)
def units_delete(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(
            request,
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

    in_use = db.execute(
        select(func.count(Product.id)).where(Product.unit_id == unit.id)
    ).scalar_one()
    if in_use:
        return RedirectResponse(url="/products/units?error=in_use", status_code=303)

    try:
        db.delete(unit)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/products/units?error=in_use", status_code=303)

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
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(request, 
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )
    _backfill_product_units(db)
    return templates.TemplateResponse(request, 
        "products/edit.html",
        {
            "request": request,
            "errors": [],
            "product": product,
            "form": _product_to_form(product),
            "options": _load_options(
                db,
                current_group_id=product.group_id,
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
    duplicate_error = _validate_product_code_unique(
        db, payload.get("code"), current_product_id=product.id
    )
    if duplicate_error:
        payload["errors"].append(duplicate_error)
    payload["errors"].extend(
        _apply_sale_type_unit_selection(
            db, payload, current_unit_id=product.unit_id
        )
    )
    if not payload["errors"] and payload["unit_id"]:
        unit = db.get(Unit, payload["unit_id"])
        if unit and unit.unit_type == "WEIGHT" and not is_allowed_weight_unit(unit.name):
            payload["errors"].append("Selected WEIGHT unit is not supported.")
    if not payload["errors"]:
        group_error = _validate_product_group_selection(
            db,
            payload.get("group_id"),
            current_group_id=product.group_id,
            required=False,
        )
        if group_error:
            payload["errors"].append(group_error)
    if payload["errors"]:
        _hydrate_effective_nominal_code_form(db, payload["form"])
        return templates.TemplateResponse(request, 
            "products/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "product": product,
                "form": payload["form"],
                "options": _load_options(
                    db,
                    current_group_id=payload.get("group_id")
                    if payload.get("group_id") is not None
                    else product.group_id,
                    current_unit_id=product.unit_id,
                    current_ewc_code_id=product.ewc_code_id,
                    current_default_destination_id=product.default_destination_id,
                ),
            },
            status_code=400,
        )

    changed_fields: list[str] = []
    if product.code != payload["code"]:
        changed_fields.append("code")
    product.code = payload["code"]
    if product.description != payload["description"]:
        changed_fields.append("description")
    product.description = payload["description"]
    if product.sales_only != payload["sales_only"]:
        changed_fields.append("sales_only")
    product.sales_only = payload["sales_only"]
    if product.group_id != payload["group_id"]:
        changed_fields.append("group_id")
    product.group_id = payload["group_id"]
    if product.unit_id != payload["unit_id"]:
        changed_fields.append("unit_id")
    product.unit_id = payload["unit_id"]
    if product.tax_rate_id != payload["tax_rate_id"]:
        changed_fields.append("tax_rate_id")
    product.tax_rate_id = payload["tax_rate_id"]
    if product.nominal_code != payload["nominal_code"]:
        changed_fields.append("nominal_code")
    product.nominal_code = payload["nominal_code"]
    if product.unit_price != payload["unit_price"]:
        changed_fields.append("unit_price")
    product.unit_price = payload["unit_price"]
    if product.account_price != payload["account_price"]:
        changed_fields.append("account_price")
    product.account_price = payload["account_price"]
    if product.cash_price != payload["cash_price"]:
        changed_fields.append("cash_price")
    product.cash_price = payload["cash_price"]
    if product.min_price != payload["min_price"]:
        changed_fields.append("min_price")
    product.min_price = payload["min_price"]
    if product.max_price != payload["max_price"]:
        changed_fields.append("max_price")
    product.max_price = payload["max_price"]
    if product.max_qty != payload["max_qty"]:
        changed_fields.append("max_qty")
    product.max_qty = payload["max_qty"]
    if product.excess_trigger != payload["excess_trigger"]:
        changed_fields.append("excess_trigger")
    product.excess_trigger = payload["excess_trigger"]
    if product.excess_price != payload["excess_price"]:
        changed_fields.append("excess_price")
    product.excess_price = payload["excess_price"]
    if product.is_hazardous != payload["is_hazardous"]:
        changed_fields.append("is_hazardous")
    product.is_hazardous = payload["is_hazardous"]
    if product.final_disposal_wip != payload["final_disposal_wip"]:
        changed_fields.append("final_disposal_wip")
    product.final_disposal_wip = payload["final_disposal_wip"]
    if product.used_on_site_wip != payload["used_on_site_wip"]:
        changed_fields.append("used_on_site_wip")
    product.used_on_site_wip = payload["used_on_site_wip"]
    if product.ewc_code_id != payload["ewc_code_id"]:
        changed_fields.append("ewc_code_id")
    product.ewc_code_id = payload["ewc_code_id"]
    if product.default_destination_id != payload["default_destination_id"]:
        changed_fields.append("default_destination_id")
    product.default_destination_id = payload["default_destination_id"]
    product.updated_at = utcnow()
    if changed_fields:
        audit_log(
            db,
            request,
            action="UPDATE",
            entity_type="product",
            entity_id=product.id,
            summary=f"Updated product {product.code}",
            details={"changed_fields": sorted(set(changed_fields))},
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        payload["errors"].append("Product code already exists.")
        _hydrate_effective_nominal_code_form(db, payload["form"])
        return templates.TemplateResponse(
            request,
            "products/edit.html",
            {
                "request": request,
                "errors": payload["errors"],
                "product": product,
                "form": payload["form"],
                "options": _load_options(
                    db,
                    current_group_id=payload.get("group_id")
                    if payload.get("group_id") is not None
                    else product.group_id,
                    current_unit_id=product.unit_id,
                    current_ewc_code_id=product.ewc_code_id,
                    current_default_destination_id=product.default_destination_id,
                ),
            },
            status_code=400,
        )
    return RedirectResponse(url="/products?saved=1", status_code=303)


@router.post("/products/{product_id:int}/delete", response_class=HTMLResponse)
def products_delete(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if not product:
        return templates.TemplateResponse(
            request,
            "products/not_found.html",
            {"request": request, "product_id": product_id},
            status_code=404,
        )

    in_use = db.execute(
        select(func.count(Ticket.id)).where(Ticket.product_id == product.id)
    ).scalar_one()
    if in_use:
        return RedirectResponse(url="/products?error=in_use", status_code=303)

    db.execute(
        delete(CustomerProductPrice).where(
            CustomerProductPrice.product_id == product.id
        )
    )
    try:
        db.delete(product)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/products?error=in_use", status_code=303)
    return RedirectResponse(url="/products?saved=1", status_code=303)


def _load_options(
    db: Session,
    current_group_id: int | None = None,
    current_unit_id: int | None = None,
    current_ewc_code_id: int | None = None,
    current_default_destination_id: int | None = None,
) -> dict[str, list[tuple[str, str]]]:
    groups = list(
        db.execute(
            select(ProductGroup)
            .where(ProductGroup.is_active.is_(True))
            .order_by(ProductGroup.name)
        ).scalars()
    )
    units = list(
        db.execute(
            select(Unit)
            .where(Unit.is_active.is_(True), Unit.unit_type == "COUNT")
            .order_by(Unit.name)
        ).scalars()
    )
    tax_rates = db.execute(select(TaxRate).order_by(TaxRate.code)).scalars()
    nominal_codes = db.execute(select(NominalCode).order_by(NominalCode.code)).scalars()
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
    group_options = [(str(row.id), row.name) for row in groups]
    if current_group_id:
        if not any(str(row.id) == str(current_group_id) for row in groups):
            current_group = db.get(ProductGroup, current_group_id)
            if current_group:
                label = (
                    f"{current_group.name} (inactive)"
                    if not current_group.is_active
                    else current_group.name
                )
                group_options = [(str(current_group.id), label)] + group_options
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
            row.code_display,
            row.description,
            row.hazardous,
            row.code_6,
        )
        for row in ewc_codes
    ]
    if current_ewc_code_id:
        if not any(str(row.id) == str(current_ewc_code_id) for row in ewc_codes):
            current = db.get(EwcCode, current_ewc_code_id)
            if current:
                description = current.description
                if not current.active:
                    description = f"{description} (inactive)"
                ewc_options = [
                    (
                        str(current.id),
                        current.code_display,
                        description,
                        current.hazardous,
                        current.code_6,
                    )
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
        "groups": group_options,
        "units": unit_options,
        "tax_rates": [
            (str(row.id), _format_tax_rate_select_label(row.rate_percent, row.code))
            for row in tax_rates
        ],
        "ewc_codes": ewc_options,
        "destinations": destination_options,
    }


def _parse_product_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    code = value("code").upper()
    description = value("description")
    group_id_raw = value("group_id")
    group_id = _parse_int(group_id_raw)
    nominal_code = _normalize_nominal_code(value("nominal_code"))
    sale_type = _normalize_sale_type(value("sale_type"))
    tax_rate_raw = value("tax_rate_id")
    tax_rate_id = _parse_int(tax_rate_raw)

    validate_no_html_fields(
        {
            "Code": code,
            "Description": description,
            "Nominal code": nominal_code,
        },
        errors,
    )

    if not code:
        errors.append("Code is required.")
    elif len(code) > CODE_MAX:
        errors.append(f"Code must be {CODE_MAX} characters or fewer.")
    if not description:
        errors.append("Description is required.")
    elif len(description) > DESC_MAX:
        errors.append(f"Description must be {DESC_MAX} characters or fewer.")
    if not sale_type:
        errors.append("Sale type is required.")
    elif sale_type not in SALE_TYPES:
        errors.append("Sale type must be WEIGHT or COUNT.")
    unit_price_raw = value("unit_price")
    if not unit_price_raw:
        errors.append("Unit price is required.")
    unit_price_value = _parse_decimal(unit_price_raw)
    if unit_price_raw and unit_price_value is None:
        errors.append("Unit price must be a number.")
    if unit_price_value is not None and unit_price_value < 0:
        errors.append("Unit price must be 0 or greater.")
    if nominal_code and len(nominal_code) > NOMINAL_CODE_MAX_LEN:
        errors.append(f"Nominal code must be {NOMINAL_CODE_MAX_LEN} characters or fewer.")

    return {
        "errors": errors,
        "form": {
            "code": code,
            "description": description,
            "group_id": group_id_raw,
            "nominal_code": nominal_code,
            "effective_nominal_code": nominal_code,
            "sales_only": "on" if value("sales_only") == "on" else "",
            "sale_type": sale_type,
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
            "ewc_code_id": value("ewc_code_id"),
            "ewc_code_label": value("ewc_code_label"),
            "default_destination_id": value("default_destination_id"),
            "is_hazardous": value("is_hazardous"),
            "final_disposal_wip": value("final_disposal_wip"),
            "used_on_site_wip": value("used_on_site_wip"),
        },
        "sale_type": sale_type,
        "code": code,
        "description": description,
        "group_id": group_id,
        "nominal_code": nominal_code or None,
        "sales_only": value("sales_only") == "on",
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
        "ewc_code_id": _parse_int(value("ewc_code_id")),
        "ewc_code_label": value("ewc_code_label"),
        "default_destination_id": _parse_int(value("default_destination_id")),
        "is_hazardous": value("is_hazardous") == "on",
        "final_disposal_wip": value("final_disposal_wip") == "on",
        "used_on_site_wip": value("used_on_site_wip") == "on",
    }


def _empty_form() -> dict:
    return {
        "code": "",
        "description": "",
        "group_id": "",
        "nominal_code": "",
        "effective_nominal_code": "",
        "sales_only": "",
        "sale_type": "",
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
        "ewc_code_id": "",
        "ewc_code_label": "",
        "default_destination_id": "",
        "is_hazardous": "",
        "final_disposal_wip": "",
        "used_on_site_wip": "",
    }


def _product_to_form(product: Product) -> dict:
    unit_type = product.unit.unit_type if product.unit else None
    sale_type = "WEIGHT" if unit_type == "WEIGHT" else "COUNT"
    effective_nominal_code = product_effective_nominal_code(product) or ""
    return {
        "code": product.code or "",
        "description": product.description or "",
        "group_id": str(product.group_id or ""),
        "nominal_code": product.nominal_code or "",
        "effective_nominal_code": effective_nominal_code,
        "sales_only": "on" if product.sales_only else "",
        "sale_type": sale_type,
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
        "ewc_code_id": str(product.ewc_code_id or ""),
        "ewc_code_label": (
            f"{product.ewc_code.code_display} - {product.ewc_code.description}"
            if product.ewc_code
            else ""
        ),
        "default_destination_id": str(product.default_destination_id or ""),
        "is_hazardous": "on" if product.is_hazardous else "",
        "final_disposal_wip": "on" if product.final_disposal_wip else "",
        "used_on_site_wip": "on" if product.used_on_site_wip else "",
    }


def _hydrate_effective_nominal_code_form(db: Session, form_data: dict) -> None:
    nominal_code = _normalize_nominal_code(str(form_data.get("nominal_code", "")))
    if nominal_code:
        form_data["effective_nominal_code"] = nominal_code
        return
    group_id = _parse_int(str(form_data.get("group_id", "")))
    if not group_id:
        form_data["effective_nominal_code"] = ""
        return
    group = db.get(ProductGroup, group_id)
    if group and group.nominal_code_default:
        form_data["effective_nominal_code"] = group.nominal_code_default
    else:
        form_data["effective_nominal_code"] = ""


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


def _format_tax_rate_select_label(rate_percent: Decimal | float | None, fallback: str) -> str:
    if rate_percent is None:
        return fallback
    try:
        raw_rate = Decimal(str(rate_percent))
    except (InvalidOperation, ValueError):
        return fallback
    if raw_rate <= Decimal("1"):
        percent = raw_rate * Decimal("100")
    else:
        percent = raw_rate
    percent_text = format(percent, "f")
    if "." in percent_text:
        percent_text = percent_text.rstrip("0").rstrip(".")
    if not percent_text:
        percent_text = "0"
    return f"{percent_text}%"


def _normalize_search_query(raw: str | None) -> str:
    if raw is None:
        return ""
    collapsed = re.sub(r"\s+", " ", str(raw).strip())
    return collapsed[:PRODUCT_SEARCH_MAX_LEN]


def _escape_like_term(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_like_pattern(value: str) -> str:
    return f"%{_escape_like_term(value)}%"


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

    kg = get_or_create("KG", "WEIGHT", aliases=["kg"])
    tonnes = get_or_create("Tonnes", "WEIGHT", aliases=["tonne", "tonnes"])
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
        description="VAT zero rate",
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
    default_tax_rate_id = system_tax_rates["standard"].id
    updated = False
    missing_tax_rates = db.execute(
        select(Product).where(Product.tax_rate_id.is_(None))
    ).scalars().all()
    for product in missing_tax_rates:
        product.tax_rate_id = default_tax_rate_id
        product.updated_at = utcnow()
        updated = True

    if updated:
        db.commit()


def _normalize_unit_name(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", str(raw).strip())


def _unit_list_order_key(unit: Unit) -> tuple[int, str, int]:
    normalized_name = normalize_unit_name(str(unit.name or ""))
    is_system_weight = (
        str(unit.unit_type or "").strip().upper() == "WEIGHT"
        and normalized_name in {"kg", "tonne", "tonnes"}
    )
    return (
        1 if is_system_weight else 0,
        normalized_name,
        int(unit.id or 0),
    )


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


def _normalize_product_group_name(raw: str | None) -> str:
    collapsed = re.sub(r"\s+", " ", str(raw or "").strip())
    return collapsed


def _normalize_nominal_code(raw: str | None) -> str:
    return str(raw or "").strip()


def _empty_product_group_form() -> dict[str, str]:
    return {
        "name": "",
        "description": "",
        "nominal_code_default": "",
    }


def _product_group_to_form(group: ProductGroup) -> dict[str, str]:
    return {
        "name": group.name or "",
        "description": group.description or "",
        "nominal_code_default": group.nominal_code_default or "",
    }


def _parse_product_group_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    name = _normalize_product_group_name(value("name"))
    description = value("description")
    nominal_code_default = _normalize_nominal_code(value("nominal_code_default"))

    validate_no_html_fields(
        {
            "Name": name,
            "Description": description,
            "Default nominal code": nominal_code_default,
        },
        errors,
    )

    if not name:
        errors.append("Name is required.")
    elif len(name) > PRODUCT_GROUP_NAME_MAX_LEN:
        errors.append(f"Name must be {PRODUCT_GROUP_NAME_MAX_LEN} characters or fewer.")
    if description and len(description) > PRODUCT_GROUP_DESCRIPTION_MAX_LEN:
        errors.append(
            f"Description must be {PRODUCT_GROUP_DESCRIPTION_MAX_LEN} characters or fewer."
        )
    if nominal_code_default and len(nominal_code_default) > NOMINAL_CODE_MAX_LEN:
        errors.append(
            f"Default nominal code must be {NOMINAL_CODE_MAX_LEN} characters or fewer."
        )

    return {
        "errors": errors,
        "form": {
            "name": name,
            "description": description,
            "nominal_code_default": nominal_code_default,
        },
        "name": name,
        "description": description or None,
        "nominal_code_default": nominal_code_default or None,
    }


def _validate_product_group_name_unique(
    db: Session,
    name: str | None,
    current_group_id: int | None = None,
) -> str | None:
    normalized = _normalize_product_group_name(name)
    if not normalized:
        return None
    query = select(ProductGroup.id).where(
        func.lower(ProductGroup.name) == normalized.lower()
    )
    if current_group_id is not None:
        query = query.where(ProductGroup.id != current_group_id)
    existing_id = db.execute(query.limit(1)).scalar_one_or_none()
    if existing_id is not None:
        return "Name already exists."
    return None


def _slugify_product_group_code(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "-", str(name or "").upper())
    slug = slug.strip("-")
    return slug[:CODE_MAX] or "GROUP"


def _build_product_group_code(db: Session, name: str) -> str:
    base = _slugify_product_group_code(name)
    candidate = base
    suffix = 2
    while True:
        existing = db.execute(
            select(ProductGroup.id).where(func.upper(ProductGroup.code) == candidate)
        ).scalar_one_or_none()
        if existing is None:
            return candidate
        suffix_token = f"-{suffix}"
        max_base_len = CODE_MAX - len(suffix_token)
        candidate = f"{base[:max_base_len]}{suffix_token}"
        suffix += 1


def _validate_product_group_selection(
    db: Session,
    group_id: int | None,
    current_group_id: int | None = None,
    *,
    required: bool = False,
) -> str | None:
    if group_id is None:
        return "Product group is required." if required else None
    group = db.get(ProductGroup, group_id)
    if not group:
        return "Product group not found."
    if not group.is_active and group_id != current_group_id:
        return "Product group is inactive."
    return None


def _validate_product_code_unique(
    db: Session, code: str | None, current_product_id: int | None = None
) -> str | None:
    normalized = str(code or "").strip().upper()
    if not normalized:
        return None
    existing_query = select(Product.id).where(func.upper(Product.code) == normalized)
    if current_product_id is not None:
        existing_query = existing_query.where(Product.id != current_product_id)
    existing_id = db.execute(existing_query.limit(1)).scalar_one_or_none()
    if existing_id is not None:
        return "Product code already exists."
    return None


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
