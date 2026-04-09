from __future__ import annotations

from contextlib import contextmanager
import logging
import shutil
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .admin_users import _active_tenant_admin_count, _tenant_user_by_id
from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..audit import user_snapshot
from ..auth import (
    hash_password,
    is_superadmin_user,
    normalize_email,
    set_user_identity_email,
    user_identity_kwargs,
    validate_email,
)
from ..db import get_db
from ..models import (
    Area,
    AuditEvent,
    CompanySetting,
    Container,
    Customer,
    CustomerAdjustment,
    CustomerProductPrice,
    Destination,
    Driver,
    Haulier,
    Invoice,
    InvoiceLine,
    InvoiceVoid,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    PrintTemplateVersion,
    Product,
    ProductGroup,
    Tenant,
    Ticket,
    TicketSequence,
    TicketVoid,
    Unit,
    User,
    UserFeedback,
    Vehicle,
    Yard,
    VehicleTare,
)
from ..models.base import utcnow
from ..seed import (
    force_refresh_system_print_templates,
    seed_print_destinations,
    seed_units,
)
from ..services.demo_tenant_reset import (
    DEMO_DEFAULT_EMAIL,
    DEMO_DEFAULT_PASSWORD,
    DEMO_RESET_INTERVAL_DAYS_MAX,
    DEMO_RESET_INTERVAL_DAYS_MIN,
    demo_reset_interval_days_value,
    demo_reset_time_minutes_value,
    format_demo_reset_datetime,
    format_demo_reset_time_input,
    next_demo_reset_at,
    parse_demo_reset_interval_days,
    parse_demo_reset_time_minutes,
    reset_demo_tenant_data,
)
from ..services.feedback import (
    FEEDBACK_STATUS_NEW,
    FEEDBACK_STATUS_READ,
    feedback_display_title,
    feedback_kind_label,
    feedback_unread_count,
)
from ..services.email_service import (
    EMAIL_PROVIDER_RESEND,
    PLATFORM_EMAIL_AUDIT_FIELDS,
    get_platform_email_settings as get_platform_email_transport_settings,
    platform_email_settings_snapshot as platform_email_transport_snapshot,
    save_platform_email_settings as save_platform_email_transport_settings,
    send_email,
    validate_platform_email_settings as validate_platform_email_transport_settings,
)
from ..timezones import UK_TIMEZONE_LABEL, uk_date_from_utc
from ..services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from ..services.platform_ai_settings import (
    ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MAX,
    ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MIN,
    ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MAX,
    ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MIN,
    AI_DASHBOARD_CACHE_TTL_MAX,
    AI_DASHBOARD_CACHE_TTL_MIN,
    AI_EXTRA_GLOBAL_INSTRUCTIONS_MAX_CHARS,
    AI_MAX_OUTPUT_TOKENS_MAX,
    AI_MAX_OUTPUT_TOKENS_MIN,
    AI_TEMPERATURE_MAX,
    AI_TEMPERATURE_MIN,
    AI_TUNING_AUDIT_FIELDS,
    DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MAX,
    DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MIN,
    DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MAX,
    DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MIN,
    SUPPORTED_ASSISTANT_FOCUS_AREAS,
    SUPPORTED_ASSISTANT_MODELS,
    SUPPORTED_ASSISTANT_RESPONSE_STYLES,
    get_platform_ai_settings,
    platform_ai_settings_defaults,
    platform_ai_settings_snapshot,
    reset_platform_ai_settings,
    save_platform_ai_settings,
    validate_platform_ai_settings,
)
from ..services.tenant_ai_settings import (
    parse_dashboard_insights_override,
    resolve_tenant_ai_settings,
)
from ..services.tenants import (
    is_demo_tenant,
    is_reserved_demo_tenant,
    normalize_subdomain,
    validate_subdomain,
)
from ..services.uploads import company_logo_upload_dir
from ..tenancy import (
    base_domain_supports_direct_tenant_hosts,
    request_platform_mode,
    tenant_external_url,
    tenant_external_url_template,
)
from ..templating import templates
from ..user_roles import ROLE_TENANT_ADMIN, TENANT_USER_ROLES, normalize_role, role_label

router = APIRouter()
logger = logging.getLogger(__name__)
_DELETE_BLOCKING_MODELS = (
    ("customer records", Customer),
    ("customer adjustments", CustomerAdjustment),
    ("customer product prices", CustomerProductPrice),
    ("products", Product),
    ("vehicles", Vehicle),
    ("vehicle tares", VehicleTare),
    ("tickets", Ticket),
    ("ticket voids", TicketVoid),
    ("invoices", Invoice),
    ("invoice lines", InvoiceLine),
    ("invoice voids", InvoiceVoid),
    ("print jobs", PrintJob),
)
_DELETE_CASCADE_MODELS = (
    PrintJob,
    CustomerProductPrice,
    CustomerAdjustment,
    VehicleTare,
    InvoiceVoid,
    TicketVoid,
    InvoiceLine,
    Ticket,
    TicketSequence,
    Invoice,
    Vehicle,
    Product,
    ProductGroup,
    Unit,
    Customer,
    Haulier,
    Driver,
    Container,
    Destination,
    Area,
    Yard,
    PrintDestination,
    PrintTemplateVersion,
    PrintTemplate,
    CompanySetting,
)


@contextmanager
def _tenant_scope(db: Session, tenant_id: int):
    previous_tenant_id = db.info.get("tenant_id")
    previous_platform_mode = db.info.get("platform_mode")
    db.info["tenant_id"] = int(tenant_id)
    db.info["platform_mode"] = False
    try:
        yield
    finally:
        db.info["tenant_id"] = previous_tenant_id
        db.info["platform_mode"] = previous_platform_mode


def _require_platform_superadmin(request: Request, db: Session) -> User:
    if not request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    user = getattr(getattr(request, "state", None), "current_user", None)
    if not isinstance(user, User):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not is_superadmin_user(db, user):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _seed_number_sequences(db: Session, tenant_id: int) -> None:
    now = utcnow()
    year = int(uk_date_from_utc(now).year)
    dialect = str(getattr(getattr(db.get_bind(), "dialect", None), "name", "") or "").lower()
    if dialect == "postgresql":
        db.execute(
            text(
                "INSERT INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
                "VALUES (:tenant_id, :year, 0, :updated_at) "
                "ON CONFLICT (tenant_id, year) DO NOTHING"
            ),
            {"tenant_id": int(tenant_id), "year": year, "updated_at": now},
        )
        db.execute(
            text(
                "INSERT INTO invoice_sequences (year, last_number, updated_at) "
                "VALUES (:year, 0, :updated_at) ON CONFLICT (year) DO NOTHING"
            ),
            {"year": year, "updated_at": now},
        )
        return

    db.execute(
        text(
            "INSERT OR IGNORE INTO ticket_sequences (tenant_id, year, last_number, updated_at) "
            "VALUES (:tenant_id, :year, 0, :updated_at)"
        ),
        {"tenant_id": int(tenant_id), "year": year, "updated_at": now},
    )
    db.execute(
        text(
            "INSERT OR IGNORE INTO invoice_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": now},
    )


def _seed_tenant_baseline(
    db: Session,
    tenant_id: int,
    *,
    company_name: str | None = None,
    include_shared_reference_data: bool = True,
) -> None:
    with _tenant_scope(db, tenant_id):
        company = ensure_company_settings_row_exists(db)
        resolved_company_name = str(company_name or "").strip()
        if resolved_company_name:
            company.name = resolved_company_name
        elif not str(company.name or "").strip():
            company.name = "Your Company Name"
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        if include_shared_reference_data:
            seed_required_reference_data(db)
        else:
            seed_units(db)
        force_refresh_system_print_templates(db)
        seed_print_destinations(db)
        company.is_initialized = True
    _seed_number_sequences(db, tenant_id)


def _user_identity(user: User | None) -> str:
    if user is None:
        return ""
    return str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip()


def _tenant_users(db: Session, tenant_id: int) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant_id))
            .order_by(User.created_at.asc(), User.email.asc(), User.id.asc())
        ).scalars()
    )


def _tenant_users_map(db: Session, tenant_ids: list[int]) -> dict[int, list[User]]:
    normalized_ids = sorted({int(tenant_id) for tenant_id in tenant_ids if tenant_id is not None})
    if not normalized_ids:
        return {}

    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id.in_(normalized_ids))
            .order_by(User.tenant_id.asc(), User.created_at.asc(), User.id.asc())
        ).scalars()
    )
    grouped: dict[int, list[User]] = {}
    for user in users:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id is None:
            continue
        grouped.setdefault(int(tenant_id), []).append(user)
    return grouped


def _tenant_summary(users: list[User]) -> dict[str, object]:
    ordered_users = list(users)
    tenant_admins = [
        user
        for user in ordered_users
        if normalize_role(getattr(user, "role", None), default="") == ROLE_TENANT_ADMIN
    ]
    initial_admin = _tenant_primary_admin(ordered_users)
    return {
        "initial_admin_email": _user_identity(initial_admin),
        "user_count": len(ordered_users),
        "active_user_count": sum(1 for user in ordered_users if bool(getattr(user, "is_active", False))),
        "tenant_admin_count": len(tenant_admins),
    }


def _tenant_primary_admin(users: list[User]) -> User | None:
    ordered_users = list(users)
    for user in ordered_users:
        if (
            normalize_role(getattr(user, "role", None), default="")
            == ROLE_TENANT_ADMIN
            and bool(getattr(user, "is_active", False))
        ):
            return user
    for user in ordered_users:
        if normalize_role(getattr(user, "role", None), default="") == ROLE_TENANT_ADMIN:
            return user
    for user in ordered_users:
        if bool(getattr(user, "is_active", False)):
            return user
    return ordered_users[0] if ordered_users else None


def _user_identity_column():
    email_col = getattr(User, "email", None)
    if email_col is not None:
        return email_col
    return getattr(User, "username")


def _tenant_role_options() -> list[tuple[str, str]]:
    return [(role, role_label(role)) for role in TENANT_USER_ROLES]


def _tenant_detail_url(tenant_id: int, **params: object) -> str:
    query_params = {
        str(key): str(value)
        for key, value in params.items()
        if value not in (None, "")
    }
    if not query_params:
        return f"/platform/tenants/{tenant_id}"
    return f"/platform/tenants/{tenant_id}?{urlencode(query_params)}"


def _tenant_detail_redirect_response(tenant_id: int, **params: object) -> RedirectResponse:
    return RedirectResponse(url=_tenant_detail_url(tenant_id, **params), status_code=303)


def _platform_tenant_or_redirect(db: Session, tenant_id: int) -> Tenant | RedirectResponse:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)
    return tenant


def _platform_tenant_user_target_or_redirect(
    db: Session,
    tenant_id: int,
    user_id: int,
) -> tuple[Tenant, User] | RedirectResponse:
    tenant_or_redirect = _platform_tenant_or_redirect(db, tenant_id)
    if isinstance(tenant_or_redirect, RedirectResponse):
        return tenant_or_redirect

    tenant = tenant_or_redirect
    user = _tenant_user_by_id(db, int(tenant.id), user_id)
    if user is None:
        return _tenant_detail_redirect_response(tenant.id, user_error="Tenant user not found.")

    primary_admin = _tenant_primary_admin(_tenant_users(db, int(tenant.id)))
    if primary_admin is not None and int(primary_admin.id) == int(user.id):
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="Primary tenant admin is managed separately above.",
        )
    return tenant, user


def _query_param_int(request: Request, key: str) -> int | None:
    raw_value = str(request.query_params.get(key, "") or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _sanitize_local_platform_return_path(target: object, *, default: str) -> str:
    parsed = urlsplit(str(target or "").strip())
    if parsed.scheme or parsed.netloc:
        return default
    path = str(parsed.path or "").strip() or default
    if not path.startswith("/") or path.startswith("//"):
        return default
    return urlunsplit(("", "", path, parsed.query, ""))


def _tenant_open_url(subdomain: str | None) -> str:
    return tenant_external_url(subdomain, path="/login")


def _tenant_access_url_template() -> str:
    return tenant_external_url_template(path="/login")


def _tenant_delete_block_reason(
    db: Session,
    tenant: Tenant,
    *,
    user_count_hint: int | None = None,
) -> str:
    if is_demo_tenant(tenant):
        return "Delete is blocked for the demo tenant because it is reserved for internal demo/testing use."

    user_count = user_count_hint
    if user_count is None:
        user_count = int(
            db.execute(
                select(func.count(User.id)).where(User.tenant_id == int(tenant.id))
            ).scalar_one()
        )
    if user_count > 1:
        return "Delete is blocked until additional tenant user accounts are removed."

    tenant_id = int(tenant.id)
    for label, model in _DELETE_BLOCKING_MODELS:
        existing = db.execute(
            select(model.id).where(model.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return f"Delete is blocked because this tenant still has {label}."
    return ""




def _tenant_form_context(
    request: Request,
    *,
    form: dict[str, str],
    errors: list[str] | None = None,
    field_errors: dict[str, str] | None = None,
) -> dict[str, object]:
    preview_subdomain = normalize_subdomain(form.get("subdomain"))
    access_url = _tenant_open_url(preview_subdomain) if preview_subdomain else _tenant_access_url_template()
    return {
        "request": request,
        "errors": errors or [],
        "field_errors": field_errors or {},
        "form": form,
        "access_url": access_url,
        "access_uses_subdomain_url": base_domain_supports_direct_tenant_hosts(),
    }


def _form_checkbox_checked(form, key: str) -> bool:
    values = list(form.getlist(key)) if hasattr(form, "getlist") else [form.get(key)]
    return any(str(value or "").strip().lower() in {"1", "true", "on", "yes"} for value in values)


def _form_value(form, key: str) -> str:
    return str(form.get(key, "") or "").strip()


def _platform_ai_settings_page_context(
    request: Request,
    *,
    settings_state,
) -> dict[str, object]:
    defaults = platform_ai_settings_defaults()
    return {
        "request": request,
        "platform_ai_settings": settings_state,
        "platform_ai_defaults": defaults,
        "platform_ai_model_options": SUPPORTED_ASSISTANT_MODELS,
        "platform_ai_response_style_options": SUPPORTED_ASSISTANT_RESPONSE_STYLES,
        "platform_ai_focus_options": SUPPORTED_ASSISTANT_FOCUS_AREAS,
        "platform_ai_temperature_min": AI_TEMPERATURE_MIN,
        "platform_ai_temperature_max": AI_TEMPERATURE_MAX,
        "platform_ai_max_tokens_min": AI_MAX_OUTPUT_TOKENS_MIN,
        "platform_ai_max_tokens_max": AI_MAX_OUTPUT_TOKENS_MAX,
        "platform_ai_cache_ttl_min": AI_DASHBOARD_CACHE_TTL_MIN,
        "platform_ai_cache_ttl_max": AI_DASHBOARD_CACHE_TTL_MAX,
        "assistant_requests_per_user_per_hour_min": ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MIN,
        "assistant_requests_per_user_per_hour_max": ASSISTANT_REQUESTS_PER_USER_PER_HOUR_MAX,
        "assistant_requests_per_tenant_per_hour_min": ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MIN,
        "assistant_requests_per_tenant_per_hour_max": ASSISTANT_REQUESTS_PER_TENANT_PER_HOUR_MAX,
        "dashboard_insights_min_refresh_seconds_min": DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MIN,
        "dashboard_insights_min_refresh_seconds_max": DASHBOARD_INSIGHTS_MIN_REFRESH_SECONDS_MAX,
        "dashboard_insights_max_per_tenant_per_hour_min": DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MIN,
        "dashboard_insights_max_per_tenant_per_hour_max": DASHBOARD_INSIGHTS_MAX_PER_TENANT_PER_HOUR_MAX,
        "platform_ai_extra_instructions_max_chars": AI_EXTRA_GLOBAL_INSTRUCTIONS_MAX_CHARS,
        "saved": request.query_params.get("saved") == "1",
        "reset_done": request.query_params.get("reset") == "1",
        "error": request.query_params.get("error", ""),
    }


def _audit_platform_ai_settings_change(
    db: Session,
    request: Request,
    *,
    current_user: User,
    action: str,
    summary: str,
    before,
    after,
) -> None:
    changed = audit_diff(
        platform_ai_settings_snapshot(before),
        platform_ai_settings_snapshot(after),
        AI_TUNING_AUDIT_FIELDS,
    )
    if not changed.get("changed"):
        return
    audit_log(
        db,
        request,
        action=action,
        entity_type="platform_setting",
        entity_id="global",
        summary=summary,
        details=changed,
        user=current_user,
        tenant_id=None,
    )


def _platform_email_settings_page_context(
    request: Request,
    *,
    settings_state,
) -> dict[str, object]:
    return {
        "request": request,
        "platform_email_settings": settings_state,
        "email_provider_label": settings_state.provider_label,
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error", ""),
        "test_sent": request.query_params.get("test_sent") == "1",
        "test_error": request.query_params.get("test_error", ""),
        "test_to": request.query_params.get("test_to", ""),
    }


def _audit_platform_email_settings_change(
    db: Session,
    request: Request,
    *,
    current_user: User,
    before,
    after,
) -> None:
    changed = audit_diff(
        platform_email_transport_snapshot(before),
        platform_email_transport_snapshot(after),
        PLATFORM_EMAIL_AUDIT_FIELDS,
    )
    if not changed.get("changed"):
        return
    audit_log(
        db,
        request,
        action="PLATFORM_EMAIL_SETTINGS_UPDATE",
        entity_type="platform_setting",
        entity_id="global",
        summary="Updated platform email settings",
        details=changed,
        user=current_user,
        tenant_id=None,
    )


@router.get("/platform/tenants", response_class=HTMLResponse)
@router.get("/admin/tenants", response_class=HTMLResponse)
def tenants_list(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)

    query = select(Tenant).order_by(Tenant.name.asc())
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(Tenant.name.ilike(like), Tenant.subdomain.ilike(like))
        )
    tenants = list(db.execute(query).scalars())
    tenant_users = _tenant_users_map(db, [int(tenant.id) for tenant in tenants])
    rows: list[dict[str, object]] = []
    active_count = 0
    disabled_count = 0
    for tenant in tenants:
        if bool(tenant.is_active):
            active_count += 1
        else:
            disabled_count += 1
        summary = _tenant_summary(tenant_users.get(int(tenant.id), []))
        delete_block_reason = _tenant_delete_block_reason(
            db,
            tenant,
            user_count_hint=int(summary["user_count"]),
        )
        rows.append(
            {
                "tenant": tenant,
                "initial_admin_email": summary["initial_admin_email"],
                "user_count": summary["user_count"],
                "active_user_count": summary["active_user_count"],
                "tenant_admin_count": summary["tenant_admin_count"],
                "open_url": _tenant_open_url(tenant.subdomain),
                "delete_allowed": not bool(delete_block_reason),
            }
        )
    return templates.TemplateResponse(
        request,
        "admin/tenants_list.html",
        {
            "request": request,
            "rows": rows,
            "q": q or "",
            "error": request.query_params.get("error", ""),
            "updated_subdomain": request.query_params.get("updated_tenant", ""),
            "updated_status": request.query_params.get("status", ""),
            "deleted_subdomain": request.query_params.get("deleted_tenant", ""),
            "tenant_count": len(tenants),
            "active_count": active_count,
            "disabled_count": disabled_count,
            "feedback_unread_count": feedback_unread_count(db),
        },
    )


@router.get("/platform/feedback", response_class=HTMLResponse)
def platform_feedback(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)

    feedback_items = db.execute(
        select(UserFeedback)
        .where(UserFeedback.status == FEEDBACK_STATUS_NEW)
        .order_by(UserFeedback.created_at.desc(), UserFeedback.id.desc())
        .limit(200)
    ).scalars().all()

    tenant_options = list(
        db.execute(select(Tenant).order_by(Tenant.name.asc(), Tenant.id.asc())).scalars()
    )
    tenant_lookup = {int(item.id): item for item in tenant_options}

    current_path = str(request.url.path or "").strip() or "/platform/feedback"

    rows: list[dict[str, object]] = []
    for item in feedback_items:
        tenant = tenant_lookup.get(int(item.tenant_id))
        tenant_name = (
            str(getattr(tenant, "name", "") or "").strip()
            or str(getattr(tenant, "subdomain", "") or "").strip()
            or f"Tenant {int(item.tenant_id)}"
        )
        tenant_subdomain = str(getattr(tenant, "subdomain", "") or "").strip()
        rows.append(
            {
                "id": int(item.id),
                "tenant_id": int(item.tenant_id),
                "tenant_name": tenant_name,
                "tenant_subdomain": tenant_subdomain,
                "tenant_open_url": _tenant_open_url(tenant_subdomain)
                if tenant_subdomain
                else "",
                "display_title": feedback_display_title(item),
                "kind": str(item.kind or ""),
                "kind_label": feedback_kind_label(item.kind),
                "message": str(item.message or ""),
                "page_url": str(item.source_path or "").strip() or "",
                "submitted_by_label": str(item.submitted_by_display_name or "").strip()
                or str(item.submitted_by_email or "").strip()
                or "Unknown user",
                "submitted_by_email": str(item.submitted_by_email or "").strip() or "",
                "created_at": item.created_at,
                "return_to": current_path,
            }
        )

    unread_count = feedback_unread_count(db)
    return templates.TemplateResponse(
        request,
        "admin/feedback.html",
        {
            "request": request,
            "rows": rows,
            "feedback_unread_count": unread_count,
            "time_zone_label": UK_TIMEZONE_LABEL,
            "feedback_marked_read": request.query_params.get("feedback_marked_read") == "1",
        },
    )


@router.post("/platform/feedback/{feedback_id:int}/read")
async def platform_feedback_mark_read(
    feedback_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    feedback = db.get(UserFeedback, int(feedback_id))
    if feedback is None:
        raise HTTPException(status_code=404, detail="Not Found")

    form = await request.form()
    return_to = _sanitize_local_platform_return_path(
        form.get("return_to"),
        default="/platform/feedback",
    )
    if str(feedback.status or "").strip().lower() == FEEDBACK_STATUS_NEW:
        feedback.status = FEEDBACK_STATUS_READ
        feedback.reviewed_at = utcnow()
        feedback.reviewed_by_user_id = int(current_user.id)

    tenant = db.get(Tenant, int(feedback.tenant_id))
    workspace_label = (
        str(getattr(tenant, "name", "") or "").strip()
        or str(getattr(tenant, "subdomain", "") or "").strip()
        or f"Tenant {int(feedback.tenant_id)}"
    )
    audit_log(
        db,
        request,
        action="USER_FEEDBACK_MARK_READ",
        entity_type="user_feedback",
        entity_id=feedback.id,
        summary=f"Marked {workspace_label} feedback #{int(feedback.id)} as read",
        details={
            "feedback_id": int(feedback.id),
            "tenant_id": int(feedback.tenant_id),
            "workspace": workspace_label,
            "marked_read_by_user_id": int(current_user.id),
            "return_to": return_to,
        },
        user=current_user,
        tenant_id=None,
    )
    db.commit()
    separator = "&" if "?" in return_to else "?"
    return RedirectResponse(
        url=f"{return_to}{separator}feedback_marked_read=1",
        status_code=303,
    )


@router.get("/platform/tenants/{tenant_id:int}", response_class=HTMLResponse)
@router.get("/admin/tenants/{tenant_id:int}", response_class=HTMLResponse)
def tenant_detail(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return HTMLResponse("Not Found", status_code=404)
    users = _tenant_users(db, int(tenant.id))
    primary_admin = _tenant_primary_admin(users)
    summary = _tenant_summary(users)
    delete_block_reason = _tenant_delete_block_reason(
        db,
        tenant,
        user_count_hint=int(summary["user_count"]),
    )
    platform_ai_settings = get_platform_ai_settings(db)
    tenant_ai_settings = resolve_tenant_ai_settings(
        ai_assistant_enabled=bool(getattr(tenant, "ai_enabled", False)),
        ai_model_override=getattr(tenant, "ai_model", None),
        dashboard_insights_override=getattr(tenant, "ai_dashboard_insights_override", None),
        platform_settings=platform_ai_settings,
    )
    demo_next_reset_at_display = format_demo_reset_datetime(next_demo_reset_at(tenant))
    return templates.TemplateResponse(
        request,
        "admin/tenant_detail.html",
        {
            "request": request,
            "tenant": tenant,
            "tenant_is_demo": is_demo_tenant(tenant),
            "tenant_is_reserved_demo": is_reserved_demo_tenant(tenant),
            "users": users,
            "summary": summary,
            "tenant_role_options": _tenant_role_options(),
            "tenant_requires_admin_role": int(summary["tenant_admin_count"]) == 0,
            "primary_admin": primary_admin,
            "primary_admin_id": int(primary_admin.id) if primary_admin is not None else None,
            "primary_admin_email": _user_identity(primary_admin),
            "editing_user_id": _query_param_int(request, "edit_user"),
            "resetting_user_id": _query_param_int(request, "reset_user"),
            "open_url": _tenant_open_url(tenant.subdomain),
            "delete_allowed": not bool(delete_block_reason),
            "delete_block_reason": delete_block_reason,
            "delete_error": request.query_params.get("delete_error", ""),
            "tenant_created": request.query_params.get("tenant_created") == "1",
            "demo_reset": request.query_params.get("demo_reset") == "1",
            "demo_reset_error": request.query_params.get("demo_reset_error", ""),
            "demo_reset_schedule_saved": request.query_params.get("demo_reset_schedule_saved") == "1",
            "demo_reset_schedule_error": request.query_params.get("demo_reset_schedule_error", ""),
            "demo_reset_interval_days": demo_reset_interval_days_value(tenant),
            "demo_reset_time_value": format_demo_reset_time_input(
                demo_reset_time_minutes_value(tenant)
            ),
            "demo_last_reset_at": getattr(tenant, "demo_last_reset_at", None),
            "demo_next_reset_at_display": demo_next_reset_at_display,
            "demo_default_email": DEMO_DEFAULT_EMAIL,
            "demo_default_password": DEMO_DEFAULT_PASSWORD,
            "demo_reset_interval_days_min": DEMO_RESET_INTERVAL_DAYS_MIN,
            "demo_reset_interval_days_max": DEMO_RESET_INTERVAL_DAYS_MAX,
            "user_saved": request.query_params.get("user_saved") == "1",
            "user_message": request.query_params.get("user_message", ""),
            "created_user_email": request.query_params.get("created_user", ""),
            "user_error": request.query_params.get("user_error", ""),
            "email_saved": request.query_params.get("email_saved") == "1",
            "email_error": request.query_params.get("email_error", ""),
            "password_saved": request.query_params.get("password_saved") == "1",
            "password_error": request.query_params.get("password_error", ""),
            "ai_saved": request.query_params.get("ai_saved") == "1",
            "ai_error": request.query_params.get("ai_error", ""),
            "assistant_default_model": platform_ai_settings.default_ai_model,
            "platform_dashboard_insights_default_enabled": (
                platform_ai_settings.ai_dashboard_insights_enabled
            ),
            "assistant_model_options": SUPPORTED_ASSISTANT_MODELS,
            "tenant_effective_ai_model": tenant_ai_settings.effective_ai_model,
            "tenant_effective_dashboard_insights_enabled": (
                tenant_ai_settings.dashboard_insights_enabled
            ),
        },
    )


@router.post("/platform/tenants/{tenant_id:int}/demo-reset-schedule")
@router.post("/admin/tenants/{tenant_id:int}/demo-reset-schedule")
async def tenant_update_demo_reset_schedule(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)
    if not is_reserved_demo_tenant(tenant):
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_schedule_error': 'Automatic demo reset is only available for the reserved demo workspace.'})}"
            ),
            status_code=303,
        )

    form = await request.form()
    try:
        interval_days = parse_demo_reset_interval_days(form.get("demo_reset_interval_days"))
        reset_time_minutes = parse_demo_reset_time_minutes(form.get("demo_reset_time"))
    except ValueError as exc:
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_schedule_error': str(exc)})}"
            ),
            status_code=303,
        )

    if interval_days is not None and reset_time_minutes is None:
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_schedule_error': 'Select a reset time when automatic reset is enabled.'})}"
            ),
            status_code=303,
        )

    previous_interval = demo_reset_interval_days_value(tenant)
    previous_time_minutes = demo_reset_time_minutes_value(tenant)
    previous_last_reset_at = getattr(tenant, "demo_last_reset_at", None)
    tenant.demo_reset_interval_days = interval_days
    tenant.demo_reset_time_minutes = reset_time_minutes
    if interval_days is not None:
        tenant.demo_last_reset_at = utcnow()

    changed: dict[str, dict[str, object]] = {}
    if previous_interval != tenant.demo_reset_interval_days:
        changed["demo_reset_interval_days"] = {
            "from": previous_interval,
            "to": tenant.demo_reset_interval_days,
        }
    if previous_time_minutes != demo_reset_time_minutes_value(tenant):
        changed["demo_reset_time"] = {
            "from": format_demo_reset_time_input(previous_time_minutes),
            "to": format_demo_reset_time_input(demo_reset_time_minutes_value(tenant)),
        }
    if previous_last_reset_at != tenant.demo_last_reset_at:
        changed["demo_last_reset_at"] = {
            "from": previous_last_reset_at.isoformat() if previous_last_reset_at else None,
            "to": tenant.demo_last_reset_at.isoformat() if tenant.demo_last_reset_at else None,
        }

    if changed:
        audit_log(
            db,
            request,
            action="TENANT_UPDATE",
            entity_type="tenant",
            entity_id=tenant.id,
            summary=f"Updated demo reset schedule for tenant {tenant.name}",
            details={"changed": changed},
            user=current_user,
            tenant_id=None,
        )
        db.commit()

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?demo_reset_schedule_saved=1",
        status_code=303,
    )


@router.get("/platform/ai-settings", response_class=HTMLResponse)
@router.get("/admin/ai-settings", response_class=HTMLResponse)
def platform_ai_settings_detail(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)
    return templates.TemplateResponse(
        request,
        "admin/platform_ai_settings.html",
        _platform_ai_settings_page_context(
            request,
            settings_state=get_platform_ai_settings(db),
        ),
    )


@router.get("/platform/tenants/new", response_class=HTMLResponse)
@router.get("/admin/tenants/new", response_class=HTMLResponse)
def tenants_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _require_platform_superadmin(request, db)

    return templates.TemplateResponse(
        request,
        "admin/tenant_form.html",
        _tenant_form_context(
            request,
            form={
                "name": "",
                "subdomain": "",
            },
        ),
    )


@router.post("/platform/ai-settings")
@router.post("/admin/ai-settings")
async def platform_ai_settings_update(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    form = await request.form()
    try:
        settings_state = validate_platform_ai_settings(
            default_ai_model=form.get("default_ai_model"),
            ai_temperature=form.get("ai_temperature"),
            ai_max_output_tokens=form.get("ai_max_output_tokens"),
            ai_dashboard_insights_enabled=_form_checkbox_checked(
                form,
                "ai_dashboard_insights_enabled",
            ),
            ai_dashboard_cache_ttl_seconds=form.get("ai_dashboard_cache_ttl_seconds"),
            assistant_requests_per_user_per_hour=form.get("assistant_requests_per_user_per_hour"),
            assistant_requests_per_tenant_per_hour=form.get("assistant_requests_per_tenant_per_hour"),
            dashboard_insights_min_refresh_seconds=form.get("dashboard_insights_min_refresh_seconds"),
            dashboard_insights_max_per_tenant_per_hour=form.get("dashboard_insights_max_per_tenant_per_hour"),
            ai_default_response_style=form.get("ai_default_response_style"),
            ai_default_focus=form.get("ai_default_focus"),
            ai_extra_global_instructions=form.get("ai_extra_global_instructions"),
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/platform/ai-settings?{urlencode({'error': str(exc)})}",
            status_code=303,
        )

    before = get_platform_ai_settings(db)
    saved = save_platform_ai_settings(db, settings_state)
    _audit_platform_ai_settings_change(
        db,
        request,
        current_user=current_user,
        action="PLATFORM_AI_SETTINGS_UPDATE",
        summary="Updated platform AI settings",
        before=before,
        after=saved,
    )
    db.commit()
    return RedirectResponse(url="/platform/ai-settings?saved=1", status_code=303)


@router.post("/platform/ai-settings/reset")
@router.post("/admin/ai-settings/reset")
def platform_ai_settings_reset_route(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    before = get_platform_ai_settings(db)
    reset_state = reset_platform_ai_settings(db)
    _audit_platform_ai_settings_change(
        db,
        request,
        current_user=current_user,
        action="PLATFORM_AI_SETTINGS_RESET",
        summary="Reset platform AI settings to defaults",
        before=before,
        after=reset_state,
    )
    db.commit()
    return RedirectResponse(url="/platform/ai-settings?reset=1", status_code=303)


@router.get("/platform/email-settings", response_class=HTMLResponse)
@router.get("/admin/email-settings", response_class=HTMLResponse)
def platform_email_settings_detail(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_platform_superadmin(request, db)
    return templates.TemplateResponse(
        request,
        "admin/platform_email_settings.html",
        _platform_email_settings_page_context(
            request,
            settings_state=get_platform_email_transport_settings(db),
        ),
    )


@router.post("/platform/email-settings")
@router.post("/admin/email-settings")
async def platform_email_settings_update(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    form = await request.form()
    try:
        settings_state = validate_platform_email_transport_settings(
            email_provider=form.get("email_provider") or EMAIL_PROVIDER_RESEND,
            from_email=form.get("from_email"),
            from_display_name=form.get("from_display_name"),
            reply_to=form.get("reply_to"),
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/platform/email-settings?{urlencode({'error': str(exc)})}",
            status_code=303,
        )

    before = get_platform_email_transport_settings(db)
    saved = save_platform_email_transport_settings(
        db,
        settings_state,
        resend_api_key=_form_value(form, "resend_api_key") or None,
    )
    _audit_platform_email_settings_change(
        db,
        request,
        current_user=current_user,
        before=before,
        after=saved,
    )
    db.commit()
    return RedirectResponse(url="/platform/email-settings?saved=1", status_code=303)


@router.post("/platform/email-settings/test")
@router.post("/admin/email-settings/test")
async def platform_email_settings_send_test(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    form = await request.form()
    test_to = normalize_email(form.get("test_to"))
    if not validate_email(test_to):
        return RedirectResponse(
            url=f"/platform/email-settings?{urlencode({'test_error': 'Enter a valid test email address.', 'test_to': test_to})}",
            status_code=303,
        )

    result = send_email(
        subject="Weighbridge Web test email",
        text_body=(
            "This is a test email from the platform email settings page.\n\n"
            "If you received this, outbound email is configured and working."
        ),
        html_body=(
            "<p>This is a test email from the platform email settings page.</p>"
            "<p>If you received this, outbound email is configured and working.</p>"
        ),
        to=[test_to],
        db=db,
    )
    audit_log(
        db,
        request,
        action="PLATFORM_TEST_EMAIL",
        entity_type="platform_setting",
        entity_id="global",
        summary=(
            f"Sent platform test email to {test_to}"
            if result.ok
            else f"Platform test email failed for {test_to}"
        ),
        details={
            "test_to": test_to,
            "status": "sent" if result.ok else "failed",
            "error": result.error if not result.ok else None,
        },
        user=current_user,
        tenant_id=None,
    )
    db.commit()
    if result.ok:
        return RedirectResponse(
            url=f"/platform/email-settings?{urlencode({'test_sent': '1', 'test_to': test_to})}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/platform/email-settings?{urlencode({'test_error': result.error or 'Test email failed.', 'test_to': test_to})}",
        status_code=303,
    )


@router.post("/platform/tenants/new", response_class=HTMLResponse)
@router.post("/admin/tenants/new", response_class=HTMLResponse)
async def tenants_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = _require_platform_superadmin(request, db)

    form = await request.form()
    name = str(form.get("name", "")).strip()
    subdomain = normalize_subdomain(form.get("subdomain"))

    errors: list[str] = []
    field_errors: dict[str, str] = {}
    if not name:
        field_errors["name"] = "Tenant name is required."
        errors.append(field_errors["name"])
    validated_subdomain, subdomain_error = validate_subdomain(subdomain)
    if subdomain_error:
        field_errors["subdomain"] = subdomain_error
        errors.append(subdomain_error)

    existing = (
        db.execute(
            select(Tenant.id).where(Tenant.subdomain == validated_subdomain).limit(1)
        ).scalar_one_or_none()
        if validated_subdomain
        else None
    )
    if existing is not None:
        field_errors["subdomain"] = "Subdomain already exists."
        errors.append(field_errors["subdomain"])

    if errors:
        return templates.TemplateResponse(
            request,
            "admin/tenant_form.html",
            _tenant_form_context(
                request,
                errors=errors,
                field_errors=field_errors,
                form={
                    "name": name,
                    "subdomain": subdomain,
                },
            ),
            status_code=400,
        )

    tenant = Tenant(name=name, subdomain=validated_subdomain, is_active=True)
    db.add(tenant)
    try:
        db.flush()
        _seed_tenant_baseline(db, int(tenant.id))

        audit_log(
            db,
            request,
            action="TENANT_CREATE",
            entity_type="tenant",
            entity_id=tenant.id,
            summary=f"Created tenant {tenant.name}",
            details={
                "subdomain": tenant.subdomain,
            },
            user=current_user,
            tenant_id=tenant.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        field_errors["subdomain"] = "Subdomain already exists."
        return templates.TemplateResponse(
            request,
            "admin/tenant_form.html",
            _tenant_form_context(
                request,
                errors=[field_errors["subdomain"]],
                field_errors=field_errors,
                form={
                    "name": name,
                    "subdomain": subdomain,
                },
            ),
            status_code=400,
        )

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?tenant_created=1",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/admin-email")
@router.post("/admin/tenants/{tenant_id:int}/admin-email")
async def tenant_update_admin_email(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)

    users = _tenant_users(db, int(tenant.id))
    primary_admin = _tenant_primary_admin(users)
    if primary_admin is None:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'email_error': 'This tenant has no user account to update yet.'})}",
            status_code=303,
        )

    form = await request.form()
    admin_email = normalize_email(form.get("admin_email"))
    if not validate_email(admin_email):
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'email_error': 'A valid tenant admin email is required.'})}",
            status_code=303,
        )

    existing_user = (
        db.execute(
            select(User)
            .where(
                User.id != int(primary_admin.id),
                User.tenant_id == int(tenant.id),
                func.lower(_user_identity_column()) == admin_email,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing_user is not None:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'email_error': 'That email is already in use for this tenant.'})}",
            status_code=303,
        )

    previous_email = _user_identity(primary_admin)
    if admin_email != previous_email:
        identity_before = user_snapshot(primary_admin)
        set_user_identity_email(primary_admin, admin_email)
        audit_log(
            db,
            request,
            action="USER_UPDATE",
            entity_type="user",
            entity_id=primary_admin.id,
            summary=f"Updated tenant admin sign-in email for {tenant.name}",
            details=audit_diff(
                identity_before,
                user_snapshot(primary_admin),
                ["username", "email"],
            ),
            user=current_user,
            tenant_id=tenant.id,
        )
        audit_log(
            db,
            request,
            action="TENANT_UPDATE",
            entity_type="tenant",
            entity_id=tenant.id,
            summary=f"Updated initial admin email for tenant {tenant.name}",
            details={
                "changed": {
                    "initial_admin_email": {
                        "from": previous_email,
                        "to": admin_email,
                    }
                }
            },
            user=current_user,
            tenant_id=tenant.id,
        )
        db.commit()

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?email_saved=1",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/users")
@router.post("/admin/tenants/{tenant_id:int}/users")
async def tenant_create_user(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)

    users = _tenant_users(db, int(tenant.id))

    form = await request.form()
    first_name = _form_value(form, "first_name")
    last_name = _form_value(form, "last_name")
    user_email = normalize_email(form.get("email") or form.get("user_email"))
    user_password = _form_value(form, "password") or _form_value(form, "user_password")
    confirm_password = _form_value(form, "confirm_password")
    user_role = normalize_role(form.get("role") or form.get("user_role"), default=None)

    if not validate_email(user_email):
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'A valid tenant user email is required.'})}",
            status_code=303,
        )
    if user_role not in TENANT_USER_ROLES:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'Select a valid tenant user role.'})}",
            status_code=303,
        )
    if len(user_password) < 8:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'Tenant user password must be at least 8 characters.'})}",
            status_code=303,
        )
    if user_password != confirm_password:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'Passwords do not match.'})}",
            status_code=303,
        )

    has_tenant_admin = any(
        normalize_role(getattr(user, "role", None), default="") == ROLE_TENANT_ADMIN
        for user in users
    )
    if not has_tenant_admin and user_role != ROLE_TENANT_ADMIN:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'The first tenant user must be a Tenant Admin.'})}",
            status_code=303,
        )

    existing_user = (
        db.execute(
            select(User.id)
            .where(
                User.tenant_id == int(tenant.id),
                func.lower(_user_identity_column()) == user_email,
            )
            .limit(1)
        ).scalar_one_or_none()
    )
    if existing_user is not None:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'That email is already in use for this tenant.'})}",
            status_code=303,
        )

    tenant_user = User(
        **user_identity_kwargs(email=user_email, role=user_role),
        first_name=first_name or None,
        last_name=last_name or None,
        password_hash=hash_password(user_password),
        is_active=True,
        tenant_id=int(tenant.id),
    )
    db.add(tenant_user)
    try:
        db.flush()
        audit_log(
            db,
            request,
            action="USER_CREATE",
            entity_type="user",
            entity_id=tenant_user.id,
            summary=f"Created tenant user for {tenant.name}",
            details={
                "first_name": first_name or None,
                "last_name": last_name or None,
                "email": user_email,
                "role": user_role,
                "is_active": True,
            },
            user=current_user,
            tenant_id=tenant.id,
        )
        audit_log(
            db,
            request,
            action="TENANT_UPDATE",
            entity_type="tenant",
            entity_id=tenant.id,
            summary=f"Added tenant user to tenant {tenant.name}",
            details={
                "created_user_first_name": first_name or None,
                "created_user_last_name": last_name or None,
                "created_user_email": user_email,
                "created_user_role": user_role,
            },
            user=current_user,
            tenant_id=tenant.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'That email is already in use for this tenant.'})}",
            status_code=303,
        )

    return RedirectResponse(
        url=(
            f"/platform/tenants/{tenant.id}?"
            f"{urlencode({'user_saved': '1', 'created_user': user_email})}"
        ),
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/users/{user_id:int}/update")
@router.post("/admin/tenants/{tenant_id:int}/users/{user_id:int}/update")
async def tenant_update_user(
    tenant_id: int,
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    target_or_redirect = _platform_tenant_user_target_or_redirect(db, tenant_id, user_id)
    if isinstance(target_or_redirect, RedirectResponse):
        return target_or_redirect
    tenant, user = target_or_redirect

    form = await request.form()
    first_name = _form_value(form, "first_name")
    last_name = _form_value(form, "last_name")
    user_role = normalize_role(form.get("role"), default=None)
    current_role = normalize_role(getattr(user, "role", None), default=None)
    current_is_active = bool(getattr(user, "is_active", False))

    if user_role not in TENANT_USER_ROLES:
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="Select a valid tenant user role.",
            edit_user=user.id,
        )

    removing_last_active_admin = (
        current_role == ROLE_TENANT_ADMIN
        and current_is_active
        and user_role != ROLE_TENANT_ADMIN
        and _active_tenant_admin_count(db, int(tenant.id), exclude_user_id=int(user.id)) == 0
    )
    if removing_last_active_admin:
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="You must keep at least one active Tenant Admin in the workspace.",
            edit_user=user.id,
        )

    identity_before = user_snapshot(user)
    user.first_name = first_name or None
    user.last_name = last_name or None
    user.role = user_role

    if current_role != user_role:
        audit_log(
            db,
            request,
            action="USER_ROLE_CHANGE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Changed role for {_user_identity(user)}",
            details={"changed": {"role": {"from": current_role, "to": user_role}}},
            user=current_user,
            tenant_id=tenant.id,
        )

    identity_changes = audit_diff(
        identity_before,
        user_snapshot(user),
        ["first_name", "last_name"],
    )
    if identity_changes["changed"]:
        audit_log(
            db,
            request,
            action="USER_UPDATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Updated tenant user details for {_user_identity(user)}",
            details=identity_changes,
            user=current_user,
            tenant_id=tenant.id,
        )

    db.commit()
    return _tenant_detail_redirect_response(tenant.id, user_message="User updated.")


@router.post("/platform/tenants/{tenant_id:int}/users/{user_id:int}/toggle-active")
@router.post("/admin/tenants/{tenant_id:int}/users/{user_id:int}/toggle-active")
async def tenant_toggle_user_active(
    tenant_id: int,
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    target_or_redirect = _platform_tenant_user_target_or_redirect(db, tenant_id, user_id)
    if isinstance(target_or_redirect, RedirectResponse):
        return target_or_redirect
    tenant, user = target_or_redirect

    current_role = normalize_role(getattr(user, "role", None), default=None)
    current_is_active = bool(getattr(user, "is_active", False))
    next_is_active = not current_is_active
    removing_last_active_admin = (
        current_role == ROLE_TENANT_ADMIN
        and current_is_active
        and not next_is_active
        and _active_tenant_admin_count(db, int(tenant.id), exclude_user_id=int(user.id)) == 0
    )
    if removing_last_active_admin:
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="You must keep at least one active Tenant Admin in the workspace.",
        )

    user.is_active = next_is_active
    audit_log(
        db,
        request,
        action="USER_ACTIVATE" if next_is_active else "USER_DEACTIVATE",
        entity_type="user",
        entity_id=user.id,
        summary=f"{'Activated' if next_is_active else 'Deactivated'} tenant user {_user_identity(user)}",
        details={"changed": {"is_active": {"from": current_is_active, "to": next_is_active}}},
        user=current_user,
        tenant_id=tenant.id,
    )
    db.commit()

    return _tenant_detail_redirect_response(
        tenant.id,
        user_message="User enabled." if next_is_active else "User disabled.",
    )


@router.post("/platform/tenants/{tenant_id:int}/users/{user_id:int}/reset-password")
@router.post("/admin/tenants/{tenant_id:int}/users/{user_id:int}/reset-password")
async def tenant_reset_user_password(
    tenant_id: int,
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)
    target_or_redirect = _platform_tenant_user_target_or_redirect(db, tenant_id, user_id)
    if isinstance(target_or_redirect, RedirectResponse):
        return target_or_redirect
    tenant, user = target_or_redirect

    form = await request.form()
    password = _form_value(form, "password")
    confirm_password = _form_value(form, "confirm_password")

    if len(password) < 8:
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="Tenant user password must be at least 8 characters.",
            reset_user=user.id,
        )
    if password != confirm_password:
        return _tenant_detail_redirect_response(
            tenant.id,
            user_error="Passwords do not match.",
            reset_user=user.id,
        )

    user.password_hash = hash_password(password)
    audit_log(
        db,
        request,
        action="USER_PASSWORD_RESET",
        entity_type="user",
        entity_id=user.id,
        summary=f"Reset password for tenant user {_user_identity(user)}",
        details={"changed": {"password": {"reset": True}}},
        user=current_user,
        tenant_id=tenant.id,
    )
    db.commit()

    return _tenant_detail_redirect_response(tenant.id, user_message="Password updated.")


@router.post("/platform/tenants/{tenant_id:int}/admin-password")
@router.post("/admin/tenants/{tenant_id:int}/admin-password")
async def tenant_update_admin_password(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)

    users = _tenant_users(db, int(tenant.id))
    primary_admin = _tenant_primary_admin(users)
    if primary_admin is None:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'password_error': 'This tenant has no user account to update yet.'})}",
            status_code=303,
        )

    form = await request.form()
    admin_password = str(form.get("admin_password", "")).strip()
    confirm_password = str(form.get("confirm_password", "")).strip()

    if len(admin_password) < 8:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'password_error': 'Tenant admin password must be at least 8 characters.'})}",
            status_code=303,
        )
    if admin_password != confirm_password:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'password_error': 'Passwords do not match.'})}",
            status_code=303,
        )

    primary_admin.password_hash = hash_password(admin_password)
    audit_log(
        db,
        request,
        action="USER_UPDATE",
        entity_type="user",
        entity_id=primary_admin.id,
        summary=f"Updated tenant admin password for {tenant.name}",
        details={"changed": {"password": {"reset": True}}},
        user=current_user,
        tenant_id=tenant.id,
    )
    audit_log(
        db,
        request,
        action="TENANT_UPDATE",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Updated initial admin password for tenant {tenant.name}",
        details={"changed": {"initial_admin_password": {"reset": True}}},
        user=current_user,
        tenant_id=tenant.id,
    )
    db.commit()

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?password_saved=1",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/ai-settings")
@router.post("/admin/tenants/{tenant_id:int}/ai-settings")
async def tenant_update_ai_settings(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)

    form = await request.form()
    ai_enabled = _form_checkbox_checked(form, "ai_enabled")
    ai_model_override = str(form.get("ai_model_override", "") or "").strip() or None
    if ai_model_override is not None and ai_model_override not in SUPPORTED_ASSISTANT_MODELS:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'ai_error': 'Select a valid AI model.'})}",
            status_code=303,
        )
    try:
        dashboard_insights_override = parse_dashboard_insights_override(
            form.get("ai_dashboard_insights_override")
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'ai_error': str(exc)})}",
            status_code=303,
        )

    previous_enabled = bool(getattr(tenant, "ai_enabled", False))
    previous_model_override = str(getattr(tenant, "ai_model", "") or "").strip() or None
    previous_dashboard_insights_override = getattr(
        tenant,
        "ai_dashboard_insights_override",
        None,
    )
    platform_ai_settings = get_platform_ai_settings(db)
    tenant.ai_enabled = ai_enabled
    tenant.ai_model = ai_model_override
    tenant.ai_dashboard_insights_override = dashboard_insights_override
    resolved_tenant_ai_settings = resolve_tenant_ai_settings(
        ai_assistant_enabled=ai_enabled,
        ai_model_override=ai_model_override,
        dashboard_insights_override=dashboard_insights_override,
        platform_settings=platform_ai_settings,
    )

    if (
        previous_enabled != ai_enabled
        or previous_model_override != ai_model_override
        or previous_dashboard_insights_override != dashboard_insights_override
    ):
        changed: dict[str, dict[str, object]] = {}
        if previous_enabled != ai_enabled:
            changed["ai_enabled"] = {
                "from": previous_enabled,
                "to": ai_enabled,
            }
        if previous_model_override != ai_model_override:
            changed["ai_model_override"] = {
                "from": previous_model_override,
                "to": ai_model_override,
                "effective": resolved_tenant_ai_settings.effective_ai_model,
            }
        if previous_dashboard_insights_override != dashboard_insights_override:
            changed["ai_dashboard_insights_override"] = {
                "from": previous_dashboard_insights_override,
                "to": dashboard_insights_override,
                "effective": resolved_tenant_ai_settings.dashboard_insights_enabled,
            }
        audit_log(
            db,
            request,
            action="TENANT_UPDATE",
            entity_type="tenant",
            entity_id=tenant.id,
            summary=f"Updated AI settings for tenant {tenant.name}",
            details={"changed": changed},
            user=current_user,
            tenant_id=tenant.id,
        )
        db.commit()

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?ai_saved=1",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/reset-demo")
@router.post("/admin/tenants/{tenant_id:int}/reset-demo")
async def tenant_reset_demo(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)
    if not is_demo_tenant(tenant):
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_error': 'Reset Demo Tenant is only available for workspaces marked as demo.'})}"
            ),
            status_code=303,
        )

    form = await request.form()
    confirmation_text = str(form.get("confirmation_text", "")).strip()
    if confirmation_text != "DEMO":
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_error': 'Type DEMO to confirm the reset.'})}"
            ),
            status_code=303,
        )

    try:
        reset_demo_tenant_data(
            db,
            request,
            tenant=tenant,
            current_user=current_user,
        )
    except Exception:
        db.rollback()
        logger.exception("Reset demo tenant failed for tenant_id=%s", tenant_id)
        return RedirectResponse(
            url=(
                f"/platform/tenants/{tenant.id}?"
                f"{urlencode({'demo_reset_error': 'Reset Demo Tenant failed. Review the server logs and try again.'})}"
            ),
            status_code=303,
        )

    return RedirectResponse(
        url=f"/platform/tenants/{tenant.id}?demo_reset=1",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/disable")
@router.post("/admin/tenants/{tenant_id:int}/disable")
def tenants_disable(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)
    tenant.is_active = False
    subdomain = str(tenant.subdomain or "").strip()
    audit_log(
        db,
        request,
        action="TENANT_DISABLE",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Disabled tenant {tenant.name}",
        details={"subdomain": tenant.subdomain},
        user=current_user,
        tenant_id=tenant.id,
    )
    db.commit()
    return RedirectResponse(
        url=f"/platform/tenants?{urlencode({'updated_tenant': subdomain, 'status': 'disabled'})}",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/enable")
@router.post("/admin/tenants/{tenant_id:int}/enable")
def tenants_enable(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)
    tenant.is_active = True
    subdomain = str(tenant.subdomain or "").strip()
    audit_log(
        db,
        request,
        action="TENANT_ENABLE",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Enabled tenant {tenant.name}",
        details={"subdomain": tenant.subdomain},
        user=current_user,
        tenant_id=tenant.id,
    )
    db.commit()
    return RedirectResponse(
        url=f"/platform/tenants?{urlencode({'updated_tenant': subdomain, 'status': 'enabled'})}",
        status_code=303,
    )


@router.post("/platform/tenants/{tenant_id:int}/delete")
@router.post("/admin/tenants/{tenant_id:int}/delete")
def tenants_delete(
    tenant_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_platform_superadmin(request, db)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return RedirectResponse(url="/platform/tenants?error=Tenant+not+found", status_code=303)

    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant.id))
            .order_by(User.id.asc())
        ).scalars()
    )
    delete_block_reason = _tenant_delete_block_reason(
        db,
        tenant,
        user_count_hint=len(users),
    )
    if delete_block_reason:
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'delete_error': delete_block_reason})}",
            status_code=303,
        )

    tenant_upload_dir = company_logo_upload_dir(int(tenant.id), create=False).parent
    subdomain = str(tenant.subdomain or "").strip()
    tenant_name = str(tenant.name or "").strip()
    user_ids = [int(user.id) for user in users if getattr(user, "id", None) is not None]

    if user_ids:
        db.execute(
            update(AuditEvent)
            .where(AuditEvent.user_id.in_(user_ids))
            .values(user_id=None)
        )

    for model in _DELETE_CASCADE_MODELS:
        db.execute(delete(model).where(model.tenant_id == int(tenant.id)))
    db.execute(delete(User).where(User.tenant_id == int(tenant.id)))

    audit_log(
        db,
        request,
        action="TENANT_DELETE",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Deleted tenant {tenant_name}",
        details={
            "subdomain": subdomain,
            "deleted_user_count": len(users),
            "hard_delete": True,
        },
        user=current_user,
        tenant_id=tenant.id,
    )
    db.delete(tenant)
    db.commit()
    shutil.rmtree(tenant_upload_dir, ignore_errors=True)
    return RedirectResponse(
        url=f"/platform/tenants?{urlencode({'deleted_tenant': subdomain})}",
        status_code=303,
    )
