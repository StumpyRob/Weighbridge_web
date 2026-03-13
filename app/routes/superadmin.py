from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import shutil
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..audit import user_snapshot
from ..auth import (
    ROLE_USER,
    ROLE_TENANT_ADMIN,
    hash_password,
    is_superadmin_user,
    normalize_email,
    set_user_identity_email,
    user_identity_kwargs,
    validate_email,
)
from ..config import settings
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
    Vehicle,
    Yard,
    VehicleTare,
)
from ..models.base import utcnow
from ..seed import seed_print_destinations, seed_print_templates, seed_units
from ..services.demo_dataset import seed_demo_dataset
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
from ..services.tenants import is_demo_tenant, normalize_subdomain, validate_subdomain
from ..services.uploads import company_logo_upload_dir
from ..tenancy import (
    base_domain_supports_direct_tenant_hosts,
    request_platform_mode,
    tenant_external_url,
    tenant_external_url_template,
)
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)
DEMO_COMPANY_NAME = "Demo Ltd."
DEMO_COMPANY_ADDRESS_LINE_1 = "1 Chapter House Street"
DEMO_COMPANY_CITY = "York"
DEMO_COMPANY_POSTCODE = "YO1 7JH"
DEMO_COMPANY_COUNTRY = "United Kingdom"
DEMO_PRIMARY_COLOR_HEX = "#2596BE"
DEMO_NAVBAR_COLOR_HEX = "#242B3B"
DEMO_LOGO_FILENAME = "demo-logo.png"
DEMO_LOGO_WEB_PATH = f"/static/uploads/company/{DEMO_LOGO_FILENAME}"
DEMO_LOGO_SOURCE = (
    Path(__file__).resolve().parents[1] / "static" / "uploads" / "company" / DEMO_LOGO_FILENAME
)
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
    year = int(now.year)
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
        seed_print_templates(db)
        seed_print_destinations(db)
        company.is_initialized = True
    _seed_number_sequences(db, tenant_id)


def _apply_demo_company_branding(db: Session, tenant_id: int) -> None:
    with _tenant_scope(db, tenant_id):
        company = ensure_company_settings_row_exists(db)
        company.name = DEMO_COMPANY_NAME
        company.address_line1 = DEMO_COMPANY_ADDRESS_LINE_1
        company.address_line2 = None
        company.city = DEMO_COMPANY_CITY
        company.postcode = DEMO_COMPANY_POSTCODE
        company.country = DEMO_COMPANY_COUNTRY
        company.navbar_color_hex = DEMO_NAVBAR_COLOR_HEX
        company.primary_color_hex = DEMO_PRIMARY_COLOR_HEX
        company.nav_logo_height_px = 34
        company.show_nav_logo = True
        company.show_nav_title = True

        if DEMO_LOGO_SOURCE.is_file():
            logo_dir = company_logo_upload_dir(tenant_id, create=True)
            logo_target = logo_dir / DEMO_LOGO_FILENAME
            shutil.copyfile(DEMO_LOGO_SOURCE, logo_target)
            company.company_logo_path = DEMO_LOGO_WEB_PATH
            company.company_logo_updated_at = utcnow()


def _user_identity(user: User | None) -> str:
    if user is None:
        return ""
    return str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip()


def _normalize_role(value: object) -> str:
    return str(value or "").strip().lower()


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
    tenant_admins = [user for user in ordered_users if _normalize_role(getattr(user, "role", "")) == ROLE_TENANT_ADMIN]
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
        if _normalize_role(getattr(user, "role", "")) == ROLE_TENANT_ADMIN:
            return user
    return ordered_users[0] if ordered_users else None


def _user_identity_column():
    email_col = getattr(User, "email", None)
    if email_col is not None:
        return email_col
    return getattr(User, "username")


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


def _reset_demo_tenant_data(
    db: Session,
    request: Request,
    *,
    tenant: Tenant,
    current_user: User,
) -> None:
    tenant_id = int(tenant.id)
    tenant_upload_dir = company_logo_upload_dir(tenant_id, create=False).parent
    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == tenant_id)
            .order_by(User.id.asc())
        ).scalars()
    )
    user_ids = [int(user.id) for user in users if getattr(user, "id", None) is not None]

    if user_ids:
        db.execute(
            update(AuditEvent)
            .where(AuditEvent.user_id.in_(user_ids))
            .values(user_id=None)
        )

    shutil.rmtree(tenant_upload_dir, ignore_errors=True)

    db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == str(tenant_id)))
    for model in _DELETE_CASCADE_MODELS:
        db.execute(delete(model).where(model.tenant_id == tenant_id))
    db.execute(delete(User).where(User.tenant_id == tenant_id))

    tenant.is_active = True
    _seed_tenant_baseline(
        db,
        tenant_id,
        company_name=str(tenant.name or "").strip(),
        include_shared_reference_data=False,
    )
    _apply_demo_company_branding(db, tenant_id)
    dataset_counts = seed_demo_dataset(db, tenant_id)
    audit_log(
        db,
        request,
        action="TENANT_RESET_DEMO",
        entity_type="tenant",
        entity_id=tenant.id,
        summary=f"Reset demo tenant {tenant.name}",
        details={
            "subdomain": tenant.subdomain,
            "deleted_user_count": len(users),
            "reseeded": True,
            "dataset": dataset_counts,
        },
        user=current_user,
        tenant_id=None,
    )
    db.commit()


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
        },
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
    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant.id))
            .order_by(User.created_at.asc(), User.username.asc(), User.id.asc())
        ).scalars()
    )
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
    return templates.TemplateResponse(
        request,
        "admin/tenant_detail.html",
        {
            "request": request,
            "tenant": tenant,
            "tenant_is_demo": is_demo_tenant(tenant),
            "users": users,
            "summary": summary,
            "primary_admin_email": _user_identity(primary_admin),
            "open_url": _tenant_open_url(tenant.subdomain),
            "delete_allowed": not bool(delete_block_reason),
            "delete_block_reason": delete_block_reason,
            "delete_error": request.query_params.get("delete_error", ""),
            "tenant_created": request.query_params.get("tenant_created") == "1",
            "demo_reset": request.query_params.get("demo_reset") == "1",
            "demo_reset_error": request.query_params.get("demo_reset_error", ""),
            "user_saved": request.query_params.get("user_saved") == "1",
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

    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant.id))
            .order_by(User.created_at.asc(), User.username.asc(), User.id.asc())
        ).scalars()
    )
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

    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant.id))
            .order_by(User.created_at.asc(), User.username.asc(), User.id.asc())
        ).scalars()
    )

    form = await request.form()
    user_email = normalize_email(form.get("user_email"))
    user_password = str(form.get("user_password", "")).strip()
    confirm_password = str(form.get("confirm_password", "")).strip()
    user_role = str(form.get("user_role", "") or "").strip().lower()

    if not validate_email(user_email):
        return RedirectResponse(
            url=f"/platform/tenants/{tenant.id}?{urlencode({'user_error': 'A valid tenant user email is required.'})}",
            status_code=303,
        )
    if user_role not in {ROLE_TENANT_ADMIN, ROLE_USER}:
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
        _normalize_role(getattr(user, "role", "")) == ROLE_TENANT_ADMIN
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
                "email": user_email,
                "role": user_role,
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

    users = list(
        db.execute(
            select(User)
            .where(User.tenant_id == int(tenant.id))
            .order_by(User.created_at.asc(), User.username.asc(), User.id.asc())
        ).scalars()
    )
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
        _reset_demo_tenant_data(
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
