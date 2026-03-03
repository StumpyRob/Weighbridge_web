from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import is_superadmin_user
from ..config import settings
from ..db import get_db
from ..models import CompanySetting, Yard
from ..services.health import collect_system_health
from ..services.print_payload import print_payload_variable_docs
from ..services.system_setup import (
    print_defaults_exist,
    required_lookup_counts,
    required_lookup_table_status,
    uploads_path_status,
)
from ..templating import templates

router = APIRouter()


@router.get("/admin/health", response_class=HTMLResponse)
def admin_health_report(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not _admin_health_enabled():
        raise HTTPException(status_code=404, detail="Not found.")

    report = collect_system_health(db)
    return templates.TemplateResponse(
        request,
        "admin/health.html",
        {
            "request": request,
            "report": report,
        },
    )


def _admin_health_enabled() -> bool:
    if settings.dev_mode or settings.debug:
        return True
    return bool(templates.env.globals.get("DEV_MODE"))


@router.post("/admin/dev-mode")
async def admin_dev_mode_toggle(request: Request) -> RedirectResponse:
    form = await request.form()
    enabled = str(form.get("enabled", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    templates.env.globals["DEV_MODE"] = bool(enabled)
    return RedirectResponse(
        url=f"/admin?dev_mode_updated=1&dev_mode={'1' if enabled else '0'}",
        status_code=303,
    )


@router.get("/admin/help")
def admin_help_root() -> RedirectResponse:
    return RedirectResponse(url="/admin/help/getting-started", status_code=303)


@router.get("/admin/help/getting-started", response_class=HTMLResponse)
def admin_help_getting_started(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/help_getting_started.html",
        {
            "request": request,
            "active_help_tab": "getting_started",
        },
    )


@router.get("/admin/help/template-variables", response_class=HTMLResponse)
def admin_help_template_variables(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/help_template_variables.html",
        {
            "request": request,
            "active_help_tab": "template_variables",
            "rows": print_payload_variable_docs(),
        },
    )


@router.get("/admin/system-status", response_class=HTMLResponse)
def admin_system_status(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = getattr(request.state, "current_user", None)
    if not is_superadmin_user(db, current_user):
        return HTMLResponse("Forbidden", status_code=403)

    company = (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )
    has_default_yard = db.execute(select(Yard.id).limit(1)).scalar_one_or_none() is not None
    lookup_counts = required_lookup_counts(db)
    lookup_schema = required_lookup_table_status(db)
    uploads = uploads_path_status()

    return templates.TemplateResponse(
        request,
        "admin/system_status.html",
        {
            "request": request,
            "initialized": bool(company and company.is_initialized),
            "has_company_setting": company is not None,
            "has_default_yard": has_default_yard,
            "lookup_counts": lookup_counts,
            "lookup_schema": lookup_schema,
            "print_defaults_ready": print_defaults_exist(db),
            "uploads": uploads,
        },
    )
