from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..auth import (
    ROLE_TENANT_ADMIN,
    hash_password,
    is_superadmin_user,
    normalize_email,
    user_identity_kwargs,
    validate_email,
)
from ..db import get_db
from ..models import Tenant, User
from ..models.base import utcnow
from ..seed import seed_print_destinations, seed_print_templates
from ..services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from ..services.tenants import normalize_subdomain, validate_subdomain
from ..tenancy import request_platform_mode
from ..templating import templates

router = APIRouter()


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


def _seed_number_sequences(db: Session) -> None:
    now = utcnow()
    year = int(now.year)
    dialect = str(getattr(getattr(db.get_bind(), "dialect", None), "name", "") or "").lower()
    if dialect == "postgresql":
        db.execute(
            text(
                "INSERT INTO ticket_sequences (year, last_number, updated_at) "
                "VALUES (:year, 0, :updated_at) ON CONFLICT (year) DO NOTHING"
            ),
            {"year": year, "updated_at": now},
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
            "INSERT OR IGNORE INTO ticket_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": now},
    )
    db.execute(
        text(
            "INSERT OR IGNORE INTO invoice_sequences (year, last_number, updated_at) "
            "VALUES (:year, 0, :updated_at)"
        ),
        {"year": year, "updated_at": now},
    )


def _seed_tenant_baseline(db: Session, tenant_id: int) -> None:
    with _tenant_scope(db, tenant_id):
        company = ensure_company_settings_row_exists(db)
        if not str(company.name or "").strip():
            company.name = "Your Company Name"
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        seed_required_reference_data(db)
        seed_print_templates(db)
        seed_print_destinations(db)
        company.is_initialized = True
    _seed_number_sequences(db)


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
    return templates.TemplateResponse(
        request,
        "admin/tenants_list.html",
        {
            "request": request,
            "tenants": tenants,
            "q": q or "",
            "saved": request.query_params.get("saved") == "1",
            "error": request.query_params.get("error", ""),
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
            .order_by(User.username.asc())
        ).scalars()
    )
    return templates.TemplateResponse(
        request,
        "admin/tenant_detail.html",
        {
            "request": request,
            "tenant": tenant,
            "users": users,
        },
    )


@router.get("/platform/tenants/new", response_class=HTMLResponse)
@router.get("/admin/tenants/new", response_class=HTMLResponse)
def tenants_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _require_platform_superadmin(request, db)

    return templates.TemplateResponse(
        request,
        "admin/tenant_form.html",
        {
            "request": request,
            "errors": [],
            "form": {
                "name": "",
                "subdomain": "",
                "admin_email": "",
                "admin_password": "",
            },
        },
    )


@router.post("/platform/tenants/new", response_class=HTMLResponse)
@router.post("/admin/tenants/new", response_class=HTMLResponse)
async def tenants_create(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = _require_platform_superadmin(request, db)

    form = await request.form()
    name = str(form.get("name", "")).strip()
    subdomain = normalize_subdomain(form.get("subdomain"))
    admin_email = normalize_email(form.get("admin_email"))
    admin_password = str(form.get("admin_password", "")).strip()

    errors: list[str] = []
    if not name:
        errors.append("Tenant name is required.")
    validated_subdomain, subdomain_error = validate_subdomain(subdomain)
    if subdomain_error:
        errors.append(subdomain_error)
    if not validate_email(admin_email):
        errors.append("A valid tenant admin email is required.")
    if len(admin_password) < 8:
        errors.append("Tenant admin password must be at least 8 characters.")

    existing = (
        db.execute(
            select(Tenant.id).where(Tenant.subdomain == validated_subdomain).limit(1)
        ).scalar_one_or_none()
        if validated_subdomain
        else None
    )
    if existing is not None:
        errors.append("Subdomain already exists.")

    if errors:
        return templates.TemplateResponse(
            request,
            "admin/tenant_form.html",
            {
                "request": request,
                "errors": errors,
                "form": {
                    "name": name,
                    "subdomain": subdomain,
                    "admin_email": admin_email,
                    "admin_password": "",
                },
            },
            status_code=400,
        )

    tenant = Tenant(name=name, subdomain=validated_subdomain, is_active=True)
    db.add(tenant)
    try:
        db.flush()
        tenant_admin = User(
            **user_identity_kwargs(email=admin_email, role=ROLE_TENANT_ADMIN),
            password_hash=hash_password(admin_password),
            is_active=True,
            tenant_id=int(tenant.id),
        )
        db.add(tenant_admin)
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
                "tenant_admin_email": admin_email,
            },
            user=current_user,
            tenant_id=tenant.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "admin/tenant_form.html",
            {
                "request": request,
                "errors": ["Subdomain or tenant admin identity already exists for this tenant."],
                "form": {
                    "name": name,
                    "subdomain": subdomain,
                    "admin_email": admin_email,
                    "admin_password": "",
                },
            },
            status_code=400,
        )

    return RedirectResponse(url="/platform/tenants?saved=1", status_code=303)


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
    return RedirectResponse(url="/platform/tenants?saved=1", status_code=303)


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
    return RedirectResponse(url="/platform/tenants?saved=1", status_code=303)
