from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..services.health import collect_system_health
from ..services.print_payload import print_payload_variable_docs
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
