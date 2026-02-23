from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..services.health import collect_system_health
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
