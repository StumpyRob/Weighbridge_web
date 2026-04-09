from __future__ import annotations

from datetime import timedelta, timezone
from urllib.parse import quote
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..auth import is_superadmin_user, user_display_name
from ..config import settings
from ..db import get_db
from ..models import AuditEvent, CompanySetting, User, UserFeedback, Yard
from ..models.base import utcnow
from ..permissions import (
    PERM_ACCESS_WORKSPACE,
    PERM_MANAGE_SETTINGS,
    PERM_MANAGE_USERS,
    permission_context_for_request,
    require_any_permission,
    require_permission,
)
from ..services.feedback import (
    FEEDBACK_EMAIL_STATUS_LABELS,
    FEEDBACK_EMAIL_STATUSES,
    FEEDBACK_KIND_LABELS,
    FEEDBACK_KINDS,
    FEEDBACK_STATUS_LABELS,
    FEEDBACK_STATUSES,
    feedback_display_title,
    feedback_email_status_label,
    feedback_kind_label,
    feedback_status_label,
    feedback_summary,
    normalize_feedback_email_status,
    normalize_feedback_kind,
    normalize_feedback_status,
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


def _sanitize_local_admin_return_path(target: object, *, default: str) -> str:
    parsed = urlsplit(str(target or "").strip())
    if parsed.scheme or parsed.netloc:
        return default
    path = str(parsed.path or "").strip() or default
    if not path.startswith("/") or path.startswith("//"):
        return default
    return urlunsplit(("", "", path, parsed.query, ""))


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


def _shared_help_back_target(request: Request) -> tuple[str, str]:
    if request_platform_mode(request):
        return "/platform/tenants", "Back to Tenant Management"
    return "/", "Back to Home"


def _help_template_context(
    request: Request,
    *,
    active_help_tab: str,
    help_base_path: str,
    help_back_href: str,
    help_back_label: str,
    platform_tools_allowed: bool,
    help_show_template_variables: bool,
    rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "request": request,
        "active_help_tab": active_help_tab,
        "platform_tools_allowed": platform_tools_allowed,
        "help_back_href": help_back_href,
        "help_back_label": help_back_label,
        "help_getting_started_href": f"{help_base_path}/getting-started",
        "help_template_variables_href": f"{help_base_path}/template-variables",
        "help_show_template_variables": help_show_template_variables,
    }
    if rows is not None:
        context["rows"] = rows
    return context


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
        _help_template_context(
            request,
            active_help_tab="getting_started",
            help_base_path="/admin/help",
            help_back_href="/admin",
            help_back_label="Back to Settings",
            platform_tools_allowed=_platform_tools_allowed(request),
            help_show_template_variables=True,
        ),
    )


@router.get("/admin/help/template-variables", response_class=HTMLResponse)
def admin_help_template_variables(request: Request) -> HTMLResponse:
    require_any_permission(request, PERM_MANAGE_SETTINGS, PERM_MANAGE_USERS)
    return templates.TemplateResponse(
        request,
        "admin/help_template_variables.html",
        _help_template_context(
            request,
            active_help_tab="template_variables",
            help_base_path="/admin/help",
            help_back_href="/admin",
            help_back_label="Back to Settings",
            platform_tools_allowed=_platform_tools_allowed(request),
            help_show_template_variables=True,
            rows=print_payload_variable_docs(),
        ),
    )


@router.get("/help")
def help_root(request: Request) -> RedirectResponse:
    require_permission(request, PERM_ACCESS_WORKSPACE)
    return RedirectResponse(url="/help/getting-started", status_code=303)


@router.get("/help/getting-started", response_class=HTMLResponse)
def help_getting_started(request: Request) -> HTMLResponse:
    require_permission(request, PERM_ACCESS_WORKSPACE)
    permissions = permission_context_for_request(request)
    back_href, back_label = _shared_help_back_target(request)
    return templates.TemplateResponse(
        request,
        "admin/help_getting_started.html",
        _help_template_context(
            request,
            active_help_tab="getting_started",
            help_base_path="/help",
            help_back_href=back_href,
            help_back_label=back_label,
            platform_tools_allowed=_platform_tools_allowed(request),
            help_show_template_variables=bool(
                permissions.manage_settings or permissions.manage_users
            ),
        ),
    )


@router.get("/help/template-variables", response_class=HTMLResponse)
def help_template_variables(request: Request) -> HTMLResponse:
    require_any_permission(request, PERM_MANAGE_SETTINGS, PERM_MANAGE_USERS)
    back_href, back_label = _shared_help_back_target(request)
    return templates.TemplateResponse(
        request,
        "admin/help_template_variables.html",
        _help_template_context(
            request,
            active_help_tab="template_variables",
            help_base_path="/help",
            help_back_href=back_href,
            help_back_label=back_label,
            platform_tools_allowed=_platform_tools_allowed(request),
            help_show_template_variables=True,
            rows=print_payload_variable_docs(),
        ),
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


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    require_permission(request, PERM_MANAGE_SETTINGS)

    selected_status = normalize_feedback_status(
        request.query_params.get("status"),
        default="",
    ) or ""
    selected_kind = normalize_feedback_kind(
        request.query_params.get("kind"),
        default="",
    ) or ""
    selected_email_status = normalize_feedback_email_status(
        request.query_params.get("email_status"),
        default="",
    ) or ""
    selected_range = str(request.query_params.get("range", "30d")).strip().lower()

    query = select(UserFeedback)
    now = utcnow()
    if selected_range == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        query = query.where(UserFeedback.created_at >= cutoff)
    elif selected_range == "7d":
        query = query.where(UserFeedback.created_at >= now - timedelta(days=7))
    elif selected_range == "30d":
        query = query.where(UserFeedback.created_at >= now - timedelta(days=30))
    elif selected_range != "all":
        selected_range = "30d"
        query = query.where(UserFeedback.created_at >= now - timedelta(days=30))

    if selected_status:
        query = query.where(UserFeedback.status == selected_status)
    if selected_kind:
        query = query.where(UserFeedback.kind == selected_kind)
    if selected_email_status:
        query = query.where(UserFeedback.email_delivery_status == selected_email_status)

    feedback_items = db.execute(
        query.order_by(UserFeedback.created_at.desc(), UserFeedback.id.desc()).limit(200)
    ).scalars().all()

    reviewer_ids = sorted(
        {int(item.reviewed_by_user_id) for item in feedback_items if item.reviewed_by_user_id is not None}
    )
    reviewer_lookup: dict[int, User] = {}
    if reviewer_ids:
        reviewer_lookup = {
            int(user.id): user
            for user in db.execute(select(User).where(User.id.in_(reviewer_ids))).scalars().all()
        }

    current_path = str(request.url.path or "").strip() or "/admin/feedback"
    if request.url.query:
        current_path = f"{current_path}?{request.url.query}"

    rows: list[dict[str, object]] = []
    for item in feedback_items:
        reviewer = (
            reviewer_lookup.get(int(item.reviewed_by_user_id))
            if item.reviewed_by_user_id is not None
            else None
        )
        reviewed_at_local = _audit_display_time(item.reviewed_at)
        created_at_local = _audit_display_time(item.created_at)
        updated_at_local = _audit_display_time(item.updated_at)
        rows.append(
            {
                "id": int(item.id),
                "display_title": feedback_display_title(item),
                "kind": str(item.kind or ""),
                "kind_label": feedback_kind_label(item.kind),
                "status": str(item.status or ""),
                "status_label": feedback_status_label(item.status),
                "message": str(item.message or ""),
                "source_path": str(item.source_path or "").strip() or "-",
                "source_title": str(item.source_title or "").strip() or "",
                "submitted_by_label": str(item.submitted_by_display_name or "").strip()
                or str(item.submitted_by_email or "").strip()
                or "Unknown user",
                "submitted_by_email": str(item.submitted_by_email or "").strip() or "",
                "host_name": str(item.host_name or "").strip() or "",
                "email_delivery_status": str(item.email_delivery_status or ""),
                "email_delivery_status_label": feedback_email_status_label(item.email_delivery_status),
                "email_delivery_error": str(item.email_delivery_error or "").strip() or "",
                "created_at_local": created_at_local,
                "updated_at_local": updated_at_local,
                "reviewed_at_local": reviewed_at_local,
                "reviewed_by_label": user_display_name(reviewer) if reviewer is not None else "",
                "status_options": [
                    {
                        "value": status_value,
                        "label": feedback_status_label(status_value),
                    }
                    for status_value in FEEDBACK_STATUSES
                ],
                "return_to": current_path,
            }
        )

    return templates.TemplateResponse(
        request,
        "admin/feedback.html",
        {
            "request": request,
            "rows": rows,
            "selected_status": selected_status,
            "selected_kind": selected_kind,
            "selected_email_status": selected_email_status,
            "selected_range": selected_range,
            "status_options": [
                {"value": item, "label": FEEDBACK_STATUS_LABELS[item]}
                for item in FEEDBACK_STATUSES
            ],
            "kind_options": [
                {"value": item, "label": FEEDBACK_KIND_LABELS[item]}
                for item in FEEDBACK_KINDS
            ],
            "email_status_options": [
                {"value": item, "label": FEEDBACK_EMAIL_STATUS_LABELS[item]}
                for item in FEEDBACK_EMAIL_STATUSES
            ],
            "feedback_summary": feedback_summary(db),
            "time_zone_label": _AUDIT_TIMEZONE_LABEL,
            "feedback_updated": request.query_params.get("feedback_updated") == "1",
            "feedback_update_status": normalize_feedback_status(
                request.query_params.get("feedback_update_status"),
                default="",
            )
            or "",
            "feedback_update_error": str(
                request.query_params.get("feedback_update_error", "")
            ).strip(),
        },
    )


@router.post("/admin/feedback/{feedback_id}/status")
async def admin_feedback_update_status(
    feedback_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    current_user = require_permission(request, PERM_MANAGE_SETTINGS)

    feedback = db.get(UserFeedback, int(feedback_id))
    if feedback is None:
        raise HTTPException(status_code=404, detail="Not Found")

    form = await request.form()
    return_to = _sanitize_local_admin_return_path(
        form.get("return_to"),
        default="/admin/feedback",
    )
    next_status = normalize_feedback_status(form.get("status"))
    if next_status is None:
        return RedirectResponse(
            url=f"{return_to}{'&' if '?' in return_to else '?'}feedback_update_error=Choose+a+valid+status.",
            status_code=303,
        )

    previous_status = str(feedback.status or "").strip().lower()
    feedback.status = next_status
    if next_status == "new":
        feedback.reviewed_at = None
        feedback.reviewed_by_user_id = None
    else:
        feedback.reviewed_at = utcnow()
        feedback.reviewed_by_user_id = int(current_user.id)

    audit_log(
        db,
        request,
        action="USER_FEEDBACK_STATUS_UPDATE",
        entity_type="user_feedback",
        entity_id=feedback.id,
        summary=(
            f"Updated feedback #{int(feedback.id)} status to {feedback_status_label(next_status)}"
        ),
        details={
            "feedback_id": int(feedback.id),
            "status": {
                "from": previous_status or None,
                "to": next_status,
            },
            "reviewed_by_user_id": int(current_user.id),
            "return_to": return_to,
        },
        user=current_user,
    )
    db.commit()
    separator = "&" if "?" in return_to else "?"
    return RedirectResponse(
        url=f"{return_to}{separator}feedback_updated=1&feedback_update_status={next_status}",
        status_code=303,
    )
