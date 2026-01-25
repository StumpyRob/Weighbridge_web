from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product, Unit

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/lookups", response_class=HTMLResponse)
def lookups_index() -> RedirectResponse:
    return RedirectResponse(url="/lookups/units", status_code=302)


@router.get("/lookups/units", response_class=HTMLResponse)
def units_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    units = db.execute(select(Unit).order_by(Unit.code)).scalars().all()
    return templates.TemplateResponse(
        "lookups/units_list.html",
        {"request": request, "units": units, "errors": []},
    )


@router.get("/lookups/units/new", response_class=HTMLResponse)
def units_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "lookups/unit_form.html",
        {"request": request, "errors": [], "form": _empty_form(), "unit": None},
    )


@router.post("/lookups/units/new", response_class=HTMLResponse)
async def units_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    form = await request.form()
    payload = _parse_unit_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "lookups/unit_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "unit": None,
            },
            status_code=400,
        )

    unit = Unit(
        code=payload["code"],
        description=payload["description"],
        is_active=payload["is_active"],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(unit)
    db.commit()
    return RedirectResponse(url="/lookups/units", status_code=303)


@router.get("/lookups/units/{unit_id}", response_class=HTMLResponse)
def units_edit(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(
            "lookups/not_found.html",
            {"request": request, "entity": "Unit", "entity_id": unit_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        "lookups/unit_form.html",
        {"request": request, "errors": [], "form": _unit_to_form(unit), "unit": unit},
    )


@router.post("/lookups/units/{unit_id}", response_class=HTMLResponse)
async def units_update(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(
            "lookups/not_found.html",
            {"request": request, "entity": "Unit", "entity_id": unit_id},
            status_code=404,
        )
    form = await request.form()
    payload = _parse_unit_form(form)
    if payload["errors"]:
        return templates.TemplateResponse(
            "lookups/unit_form.html",
            {
                "request": request,
                "errors": payload["errors"],
                "form": payload["form"],
                "unit": unit,
            },
            status_code=400,
        )
    unit.code = payload["code"]
    unit.description = payload["description"]
    unit.is_active = payload["is_active"]
    unit.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/lookups/units", status_code=303)


@router.post("/lookups/units/{unit_id}/delete", response_class=HTMLResponse)
def units_delete(
    unit_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if not unit:
        return templates.TemplateResponse(
            "lookups/not_found.html",
            {"request": request, "entity": "Unit", "entity_id": unit_id},
            status_code=404,
        )

    in_use = db.execute(
        select(func.count(Product.id)).where(Product.unit_id == unit.id)
    ).scalar()
    if in_use and in_use > 0:
        units = db.execute(select(Unit).order_by(Unit.code)).scalars().all()
        return templates.TemplateResponse(
            "lookups/units_list.html",
            {
                "request": request,
                "units": units,
                "errors": ["Cannot delete unit that is referenced by products."],
            },
            status_code=400,
        )

    db.delete(unit)
    db.commit()
    return RedirectResponse(url="/lookups/units", status_code=303)


def _parse_unit_form(form) -> dict:
    def value(key: str) -> str:
        return str(form.get(key, "")).strip()

    errors: list[str] = []
    code = value("code").upper()
    if not code:
        errors.append("Code is required.")

    return {
        "errors": errors,
        "form": {
            "code": code,
            "description": value("description"),
            "is_active": value("is_active"),
        },
        "code": code,
        "description": value("description") or None,
        "is_active": value("is_active") == "on",
    }


def _empty_form() -> dict:
    return {"code": "", "description": "", "is_active": "on"}


def _unit_to_form(unit: Unit) -> dict:
    return {
        "code": unit.code or "",
        "description": unit.description or "",
        "is_active": "on" if unit.is_active else "",
    }
