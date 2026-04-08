from __future__ import annotations

from datetime import timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import is_superadmin_user, user_display_name
from ..config import settings
from ..db import get_db
from ..models import AuditEvent, CompanySetting, User, Yard
from ..models.base import utcnow
from ..permissions import (
    PERM_MANAGE_SETTINGS,
    PERM_MANAGE_USERS,
    require_any_permission,
    require_permission,
)
from ..services.health import collect_system_health
from ..services.print_payload import print_payload_variable_docs
from ..services.system_setup import (
    print_defaults_exist,
    required_lookup_counts,
    required_lookup_table_status,
    uploads_path_status,
)
from ..services.ui_branding import get_branding
from ..tenancy import request_platform_mode
from ..templating import templates

router = APIRouter()
_AUDIT_TIMEZONE = ZoneInfo("Europe/London")
_AUDIT_TIMEZONE_LABEL = "Europe/London"


def _platform_tools_allowed(request: Request) -> bool:
    return request_platform_mode(request) or bool(
        getattr(getattr(request, "state", None), "legacy_single_host", False)
    )


def _require_platform_superadmin(request: Request, db: Session) -> None:
    if not _platform_tools_allowed(request):
        raise HTTPException(status_code=404, detail="Not Found")
    current_user = getattr(request.state, "current_user", None)
    if not is_superadmin_user(db, current_user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _audit_display_time(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_AUDIT_TIMEZONE)


def _audit_entity_link(entity_type: str, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    normalized = str(entity_type or "").strip().lower()
    encoded = quote(str(entity_id), safe="")
    route_map = {
        "ticket": "/tickets/{id}",
        "invoice": "/invoices/{id}",
        "customer": "/customers/{id}",
        "product": "/products/{id}",
    }
    template = route_map.get(normalized)
    if not template:
        return None
    return template.format(id=encoded)


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
async def admin_dev_mode_toggle(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_platform_superadmin(request, db)
    form = await request.form()
    enabled = str(form.get("enabled", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    templates.env.globals["DEV_MODE"] = bool(enabled)
    return RedirectResponse(
        url="/platform/tenants" if request_platform_mode(request) else "/admin",
        status_code=303,
    )


@router.get("/admin/help")
def admin_help_root(request: Request) -> RedirectResponse:
    require_any_permission(request, PERM_MANAGE_SETTINGS, PERM_MANAGE_USERS)
    return RedirectResponse(url="/admin/help/getting-started", status_code=303)


@router.get("/admin/help/getting-started", response_class=HTMLResponse)
def admin_help_getting_started(request: Request) -> HTMLResponse:
    require_any_permission(request, PERM_MANAGE_SETTINGS, PERM_MANAGE_USERS)
    return templates.TemplateResponse(
        request,
        "admin/help_getting_started.html",
        {
            "request": request,
            "active_help_tab": "getting_started",
            "platform_tools_allowed": _platform_tools_allowed(request),
        },
    )


@router.get("/admin/help/template-variables", response_class=HTMLResponse)
def admin_help_template_variables(request: Request) -> HTMLResponse:
    require_any_permission(request, PERM_MANAGE_SETTINGS, PERM_MANAGE_USERS)
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
    _require_platform_superadmin(request, db)

    company = (
        db.execute(select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1))
        .scalars()
        .first()
    )
    has_default_yard = db.execute(select(Yard.id).limit(1)).scalar_one_or_none() is not None
    lookup_counts = required_lookup_counts(db)
    lookup_schema = required_lookup_table_status(db)
    uploads = uploads_path_status()
    branding = get_branding(db)

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
            "branding_status": {
                "logo_url": str(branding.get("logo_url", "") or ""),
                "logo_exists": bool(branding.get("logo_exists", False)),
                "nav_color": str(branding.get("nav_color", "") or ""),
                "primary_color": str(branding.get("primary_color", "") or ""),
            },
        },
    )


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    require_permission(request, PERM_MANAGE_SETTINGS)

    selected_entity_type = str(request.query_params.get("entity_type", "")).strip().lower()
    selected_action = str(request.query_params.get("action", "")).strip().upper()
    selected_entity_id = str(request.query_params.get("entity_id", "")).strip()
    selected_range = str(request.query_params.get("range", "7d")).strip().lower()
    selected_user_raw = str(request.query_params.get("user_id", "")).strip()
    selected_user_id = int(selected_user_raw) if selected_user_raw.isdigit() else None

    query = select(AuditEvent)
    option_filters = []
    tenant_id = getattr(request.state, "tenant_id", None)
    if not request_platform_mode(request) and tenant_id is not None:
        tenant_filter = AuditEvent.tenant_id == str(int(tenant_id))
        option_filters.append(tenant_filter)
        query = query.where(tenant_filter)

    now = utcnow()
    if selected_range == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.where(AuditEvent.occurred_at >= cutoff)
    elif selected_range == "30d":
        query = query.where(AuditEvent.occurred_at >= now - timedelta(days=30))
    elif selected_range == "7d":
        query = query.where(AuditEvent.occurred_at >= now - timedelta(days=7))

    if selected_entity_type:
        query = query.where(AuditEvent.entity_type == selected_entity_type)
    if selected_action:
        query = query.where(AuditEvent.action == selected_action)
    if selected_user_id is not None:
        query = query.where(AuditEvent.user_id == selected_user_id)
    if selected_entity_id:
        query = query.where(AuditEvent.entity_id == selected_entity_id)

    events = db.execute(
        query.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(200)
    ).scalars().all()

    referenced_user_ids = sorted(
        {int(event.user_id) for event in events if event.user_id is not None}
    )
    user_lookup: dict[int, User] = {}
    if referenced_user_ids:
        users = db.execute(
            select(User).where(User.id.in_(referenced_user_ids))
        ).scalars().all()
        user_lookup = {int(item.id): item for item in users}

    event_rows = []
    for event in events:
        event_user = user_lookup.get(int(event.user_id)) if event.user_id is not None else None
        occurred_at_local = _audit_display_time(event.occurred_at)
        event_rows.append(
            {
                "id": event.id,
                "occurred_at": event.occurred_at,
                "occurred_at_local": occurred_at_local,
                "user_label": user_display_name(event_user)
                if event_user is not None
                else ("System" if event.user_id is None else f"User {event.user_id}"),
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "summary": event.summary,
                "entity_href": _audit_entity_link(event.entity_type, event.entity_id),
            }
        )

    entity_type_options = db.execute(
        select(AuditEvent.entity_type)
        .where(*option_filters)
        .distinct()
        .order_by(AuditEvent.entity_type.asc())
    ).scalars().all()
    action_options = db.execute(
        select(AuditEvent.action)
        .where(*option_filters)
        .distinct()
        .order_by(AuditEvent.action.asc())
    ).scalars().all()
    user_options = db.execute(
        select(User)
        .join(AuditEvent, AuditEvent.user_id == User.id)
        .where(*option_filters)
        .distinct()
        .order_by(User.username.asc())
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "request": request,
            "rows": event_rows,
            "entity_type_options": entity_type_options,
            "action_options": action_options,
            "user_options": user_options,
            "selected_entity_type": selected_entity_type,
            "selected_action": selected_action,
            "selected_user_id": selected_user_id,
            "selected_range": selected_range,
            "selected_entity_id": selected_entity_id,
            "time_zone_label": _AUDIT_TIMEZONE_LABEL,
        },
    )
