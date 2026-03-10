from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..audit import user_snapshot
from ..auth import (
    ROLE_SUPERADMIN,
    SESSION_PLATFORM_MODE_KEY,
    SESSION_ROLE_KEY,
    SESSION_TENANT_ID_KEY,
    SESSION_USER_ID_KEY,
    ensure_user_role,
    hash_password,
    normalize_email,
    user_identity_kwargs,
    user_by_email,
    validate_email,
    verify_password,
)
from ..db import get_db
from ..models import User
from ..tenancy import platform_route_url, request_platform_mode
from ..templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _safe_next_path(raw: str | None) -> str:
    candidate = str(raw or "").strip()
    if not candidate.startswith("/"):
        return "/tickets"
    if candidate.startswith("//"):
        return "/tickets"
    if candidate.startswith("/login"):
        return "/tickets"
    if candidate.startswith("/logout"):
        return "/tickets"
    return candidate


def _default_next_path(request: Request) -> str:
    if request_platform_mode(request):
        return "/platform/tenants"
    return "/tickets"


def _login_context(
    request: Request,
    *,
    errors: list[str] | None = None,
    email: str = "",
    next_path: str = "/tickets",
    no_users_configured: bool = False,
    no_users_message: str = "",
    bootstrap_url: str = "",
) -> dict[str, object]:
    return {
        "request": request,
        "errors": errors or [],
        "email": email,
        "next": next_path,
        "bootstrap_path": _bootstrap_path(request),
        "bootstrap_url": bootstrap_url,
        "logged_out": request.query_params.get("logged_out") == "1",
        "bootstrapped": request.query_params.get("bootstrap") == "1",
        "no_users_configured": no_users_configured,
        "no_users_message": no_users_message,
    }


def _request_tenant_id(request: Request) -> int | None:
    tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
    if tenant_id is None:
        return None
    try:
        return int(tenant_id)
    except (TypeError, ValueError):
        return None


def _legacy_single_host_mode(request: Request) -> bool:
    return bool(getattr(getattr(request, "state", None), "legacy_single_host", False))


def _identity_column():
    email_col = getattr(User, "email", None)
    if email_col is not None:
        return email_col
    return getattr(User, "username")


def _login_user_lookup(db: Session, request: Request, email: str) -> User | None:
    if request_platform_mode(request):
        identity_col = _identity_column()
        return (
            db.execute(
                select(User).where(
                    func.lower(identity_col) == email,
                    User.tenant_id.is_(None),
                )
            )
            .scalars()
            .first()
        )

    legacy_single_host = _legacy_single_host_mode(request)
    if legacy_single_host:
        return user_by_email(
            db,
            email,
            skip_tenant_scope=True,
        )

    tenant_id = _request_tenant_id(request)
    if tenant_id is None:
        return None
    identity_col = _identity_column()
    return (
        db.execute(
            select(User).where(
                func.lower(identity_col) == email,
                User.tenant_id == int(tenant_id),
            )
        )
        .scalars()
        .first()
    )


def _login_scope_user_count(db: Session, request: Request) -> int:
    if request_platform_mode(request):
        return int(
            db.execute(
                select(func.count(User.id)).where(
                    func.lower(func.trim(User.role)) == ROLE_SUPERADMIN,
                    User.tenant_id.is_(None),
                )
            ).scalar_one_or_none()
            or 0
        )

    if _legacy_single_host_mode(request):
        return int(
            db.execute(
                select(func.count(User.id)).execution_options(skip_tenant_scope=True)
            ).scalar_one_or_none()
            or 0
        )

    tenant_id = _request_tenant_id(request)
    if tenant_id is None:
        return 0
    return int(
        db.execute(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.role != ROLE_SUPERADMIN,
            )
        ).scalar_one_or_none()
        or 0
    )


def _platform_superadmin_count(db: Session) -> int:
    return int(
        db.execute(
            select(func.count(User.id))
            .execution_options(skip_tenant_scope=True)
            .where(
                func.lower(func.trim(User.role)) == ROLE_SUPERADMIN,
                User.tenant_id.is_(None),
            )
        ).scalar_one_or_none()
        or 0
    )


def _bootstrap_enabled(db: Session, request: Request) -> bool:
    if request_platform_mode(request):
        return _platform_superadmin_count(db) == 0
    if not _legacy_single_host_mode(request):
        return False
    return _platform_superadmin_count(db) == 0


def _bootstrap_entry_url(db: Session, request: Request) -> str:
    if _platform_superadmin_count(db) != 0:
        return ""
    if request_platform_mode(request) or _legacy_single_host_mode(request):
        return _bootstrap_path(request)
    return platform_route_url(request, path="/platform/bootstrap")


def _no_users_message(db: Session, request: Request) -> tuple[str, str]:
    bootstrap_url = _bootstrap_entry_url(db, request)
    if request_platform_mode(request) or _legacy_single_host_mode(request):
        return (
            "No user accounts exist yet. Create the first platform administrator to finish setup.",
            bootstrap_url,
        )

    if bootstrap_url:
        return (
            "No user accounts exist yet. Create the first platform administrator to finish setup.",
            bootstrap_url,
        )

    return (
        "No user accounts exist for this workspace yet. Ask a platform administrator to create the first workspace admin.",
        "",
    )


def _user_allowed_for_scope(user: User, request: Request, *, role: str) -> bool:
    tenant_id = _request_tenant_id(request)
    legacy_single_host = _legacy_single_host_mode(request)
    if request_platform_mode(request):
        return role == ROLE_SUPERADMIN and user.tenant_id is None
    if tenant_id is None:
        return False
    if role == ROLE_SUPERADMIN:
        return legacy_single_host and user.tenant_id is None
    if user.tenant_id is None:
        return legacy_single_host
    return int(user.tenant_id) == int(tenant_id)


def _bootstrap_context(
    request: Request,
    *,
    errors: list[str] | None = None,
    email: str = "",
) -> dict[str, object]:
    return {
        "request": request,
        "errors": errors or [],
        "email": email,
        "bootstrap_path": _bootstrap_path(request),
    }


def _bootstrap_path(request: Request) -> str:
    if request_platform_mode(request):
        return "/platform/bootstrap"
    return "/bootstrap"


def _bootstrap_login_redirect_path(request: Request) -> str:
    params: dict[str, str] = {"bootstrap": "1"}
    if request_platform_mode(request):
        params["next"] = "/platform/tenants"
    return f"/login?{urlencode(params)}"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current = getattr(request.state, "current_user", None)
    if isinstance(current, User) and current.is_active:
        return RedirectResponse(url=_default_next_path(request), status_code=303)

    user_count = _login_scope_user_count(db, request)
    next_raw = request.query_params.get("next")
    next_path = _safe_next_path(next_raw) if next_raw else _default_next_path(request)
    no_users_message, bootstrap_url = (
        _no_users_message(db, request) if user_count == 0 else ("", "")
    )
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        _login_context(
            request,
            next_path=next_path,
            no_users_configured=user_count == 0,
            no_users_message=no_users_message,
            bootstrap_url=bootstrap_url,
        ),
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current = getattr(request.state, "current_user", None)
    if isinstance(current, User) and current.is_active:
        return RedirectResponse(url=_default_next_path(request), status_code=303)

    form = await request.form()
    email = normalize_email(str(form.get("email", "")))
    password = str(form.get("password", ""))
    next_raw = form.get("next")
    next_path = _safe_next_path(next_raw) if next_raw else _default_next_path(request)
    scope_user_count = _login_scope_user_count(db, request)
    no_users_message, bootstrap_url = (
        _no_users_message(db, request) if scope_user_count == 0 else ("", "")
    )

    errors: list[str] = []
    if not validate_email(email):
        errors.append("Enter a valid email address.")
    if not password:
        errors.append("Password is required.")

    if errors:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            _login_context(
                request,
                errors=errors,
                email=email,
                next_path=next_path,
                no_users_configured=scope_user_count == 0,
                no_users_message=no_users_message,
                bootstrap_url=bootstrap_url,
            ),
            status_code=400,
        )

    legacy_single_host = _legacy_single_host_mode(request)
    user = _login_user_lookup(db, request, email)
    role = ensure_user_role(db, user, allow_bootstrap=True) if user is not None else ""
    if (
        user is None
        or not user.is_active
        or not _user_allowed_for_scope(user, request, role=role)
        or not verify_password(password, user.password_hash)
    ):
        audit_log(
            db,
            request,
            action="LOGIN_FAILED",
            entity_type="auth",
            entity_id=email or None,
            summary="Login failed",
            details={"reason": "invalid_credentials"},
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            _login_context(
                request,
                errors=["Invalid email or password."],
                email=email,
                next_path=next_path,
                no_users_configured=scope_user_count == 0,
                no_users_message=no_users_message,
                bootstrap_url=bootstrap_url,
            ),
            status_code=401,
        )

    request_tenant_id = _request_tenant_id(request)
    if (
        not request_platform_mode(request)
        and role != ROLE_SUPERADMIN
        and request_tenant_id is not None
        and getattr(user, "tenant_id", None) is None
    ):
        user.tenant_id = int(request_tenant_id)

    audit_log(
        db,
        request,
        action="LOGIN_SUCCESS",
        entity_type="user",
        entity_id=user.id,
        summary="User logged in",
        details={
            "username": str(getattr(user, "username", "") or "").strip() or None,
        },
        user=user,
    )
    db.commit()
    request.session[SESSION_USER_ID_KEY] = int(user.id)
    request.session[SESSION_ROLE_KEY] = role
    request.session[SESSION_PLATFORM_MODE_KEY] = bool(request_platform_mode(request))
    if request_platform_mode(request):
        request.session[SESSION_TENANT_ID_KEY] = None
    elif role == ROLE_SUPERADMIN and legacy_single_host and request_tenant_id is not None:
        request.session[SESSION_TENANT_ID_KEY] = int(request_tenant_id)
    else:
        request.session[SESSION_TENANT_ID_KEY] = int(getattr(user, "tenant_id", 0) or 0)
    return RedirectResponse(url=next_path, status_code=303)


@router.get("/platform/bootstrap", response_class=HTMLResponse)
@router.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _bootstrap_enabled(db, request):
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(
        request,
        "auth/bootstrap.html",
        _bootstrap_context(request),
    )


@router.post("/platform/bootstrap", response_class=HTMLResponse)
@router.post("/bootstrap", response_class=HTMLResponse)
async def bootstrap_submit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _bootstrap_enabled(db, request):
        return HTMLResponse("Not Found", status_code=404)

    form = await request.form()
    email = normalize_email(str(form.get("email", "")))
    password = str(form.get("password", ""))
    confirm_password = str(form.get("confirm_password", ""))

    errors: list[str] = []
    if not validate_email(email):
        errors.append("Enter a valid email address.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm_password:
        errors.append("Passwords do not match.")

    if errors:
        return templates.TemplateResponse(
            request,
            "auth/bootstrap.html",
            _bootstrap_context(request, errors=errors, email=email),
            status_code=400,
        )

    if user_by_email(db, email) is not None:
        return templates.TemplateResponse(
            request,
            "auth/bootstrap.html",
            _bootstrap_context(
                request,
                errors=["A user with this email already exists."],
                email=email,
            ),
            status_code=400,
        )

    user = User(
        **user_identity_kwargs(email=email, role=ROLE_SUPERADMIN),
        password_hash=hash_password(password),
        is_active=True,
        tenant_id=None,
    )
    db.add(user)
    try:
        db.flush()
        snapshot_after = user_snapshot(user)
        audit_log(
            db,
            request,
            action="USER_CREATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Created user {snapshot_after.get('email') or snapshot_after.get('username') or user.id}",
            details=audit_diff(
                {},
                snapshot_after,
                ["username", "email", "role", "is_active"],
            ),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return HTMLResponse("Not Found", status_code=404)

    logger.info("Bootstrap superadmin created via web for email=%s", email)
    return RedirectResponse(url=_bootstrap_login_redirect_path(request), status_code=303)


@router.post("/logout")
def logout_submit(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    if isinstance(current_user, User):
        audit_log(
            db,
            request,
            action="LOGOUT",
            entity_type="user",
            entity_id=current_user.id,
            summary="User logged out",
            details={
                "username": str(getattr(current_user, "username", "") or "").strip() or None,
            },
            user=current_user,
        )
        db.commit()
    request.session.clear()
    return RedirectResponse(
        url=f"/login?{urlencode({'logged_out': '1'})}",
        status_code=303,
    )
