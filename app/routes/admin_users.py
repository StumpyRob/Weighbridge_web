from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..audit import user_snapshot
from ..auth import (
    hash_password,
    normalize_email,
    set_user_identity_email,
    user_identity_kwargs,
    validate_email,
)
from ..db import get_db
from ..models import User
from ..permissions import PERM_MANAGE_USERS, require_permission
from ..templating import templates
from ..tenancy import request_platform_mode
from ..user_roles import (
    ROLE_TENANT_ADMIN,
    TENANT_USER_ROLES,
    normalize_role,
    role_label,
)

router = APIRouter()


def _require_tenant_user_admin(request: Request) -> User:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return require_permission(request, PERM_MANAGE_USERS)


def _current_tenant_id(request: Request) -> int:
    tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return int(tenant_id)


def _tenant_user_query(tenant_id: int):
    return (
        select(User)
        .where(User.tenant_id == int(tenant_id))
        .order_by(User.created_at.asc(), User.email.asc(), User.id.asc())
    )


def _tenant_users(db: Session, tenant_id: int) -> list[User]:
    return list(db.execute(_tenant_user_query(tenant_id)).scalars())


def _role_options() -> list[tuple[str, str]]:
    return [(role, role_label(role)) for role in TENANT_USER_ROLES]


def _active_tenant_admin_count(db: Session, tenant_id: int, *, exclude_user_id: int | None = None) -> int:
    query = select(func.count(User.id)).where(
        User.tenant_id == int(tenant_id),
        func.lower(func.trim(User.role)) == ROLE_TENANT_ADMIN,
        User.is_active.is_(True),
    )
    if exclude_user_id is not None:
        query = query.where(User.id != int(exclude_user_id))
    return int(db.execute(query).scalar_one_or_none() or 0)


def _tenant_user_by_id(db: Session, tenant_id: int, user_id: int) -> User | None:
    return (
        db.execute(
            select(User).where(
                User.id == int(user_id),
                User.tenant_id == int(tenant_id),
            )
        )
        .scalars()
        .first()
    )


def _user_identity_column():
    email_col = getattr(User, "email", None)
    if email_col is not None:
        return email_col
    return getattr(User, "username")


def _tenant_user_management_url(
    tenant_id: int,
    *,
    message: str | None = None,
    error: str | None = None,
) -> str:
    params: dict[str, str] = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    if not params:
        return "/admin/users"
    return f"/admin/users?{urlencode(params)}"


def _user_form_value(form, key: str) -> str:
    return str(form.get(key, "") or "").strip()


def _checkbox_checked(form, key: str) -> bool:
    values = list(form.getlist(key)) if hasattr(form, "getlist") else [form.get(key)]
    return any(str(value or "").strip().lower() in {"1", "true", "on", "yes"} for value in values)


def _existing_user_with_email(
    db: Session,
    *,
    tenant_id: int,
    email: str,
    exclude_user_id: int | None = None,
) -> User | None:
    query = select(User).where(
        User.tenant_id == int(tenant_id),
        func.lower(_user_identity_column()) == email,
    )
    if exclude_user_id is not None:
        query = query.where(User.id != int(exclude_user_id))
    return db.execute(query.limit(1)).scalars().first()


def _user_management_context(
    request: Request,
    *,
    users: list[User],
) -> dict[str, object]:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    return {
        "request": request,
        "users": users,
        "role_options": _role_options(),
        "current_user_id": int(getattr(current_user, "id", 0) or 0) or None,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_list(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_user_admin(request)
    tenant_id = _current_tenant_id(request)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _user_management_context(request, users=_tenant_users(db, tenant_id)),
    )


@router.post("/admin/users", response_class=HTMLResponse)
async def admin_users_create(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    acting_user = _require_tenant_user_admin(request)
    tenant_id = _current_tenant_id(request)
    form = await request.form()
    first_name = _user_form_value(form, "first_name")
    last_name = _user_form_value(form, "last_name")
    email = normalize_email(form.get("email"))
    role = normalize_role(form.get("role"), default=None)
    password = _user_form_value(form, "password")
    confirm_password = _user_form_value(form, "confirm_password")

    if not validate_email(email):
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="A valid email address is required."),
            status_code=303,
        )
    if role not in TENANT_USER_ROLES:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Select a valid tenant role."),
            status_code=303,
        )
    if len(password) < 8:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Password must be at least 8 characters."),
            status_code=303,
        )
    if password != confirm_password:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Passwords do not match."),
            status_code=303,
        )
    if _active_tenant_admin_count(db, tenant_id) == 0 and role != ROLE_TENANT_ADMIN:
        return RedirectResponse(
            url=_tenant_user_management_url(
                tenant_id,
                error="The first active workspace user must be a Tenant Admin.",
            ),
            status_code=303,
        )
    if _existing_user_with_email(db, tenant_id=tenant_id, email=email) is not None:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="That email is already in use for this workspace."),
            status_code=303,
        )

    tenant_user = User(
        **user_identity_kwargs(email=email, role=role),
        first_name=first_name or None,
        last_name=last_name or None,
        password_hash=hash_password(password),
        is_active=True,
        tenant_id=tenant_id,
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
            summary=f"Created workspace user {email}",
            details={
                "email": email,
                "first_name": first_name or None,
                "last_name": last_name or None,
                "role": role,
                "is_active": True,
            },
            user=acting_user,
            tenant_id=tenant_id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="That email is already in use for this workspace."),
            status_code=303,
        )

    return RedirectResponse(
        url=_tenant_user_management_url(tenant_id, message="User created."),
        status_code=303,
    )


@router.post("/admin/users/{user_id:int}/update", response_class=HTMLResponse)
async def admin_users_update(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    acting_user = _require_tenant_user_admin(request)
    tenant_id = _current_tenant_id(request)
    user = _tenant_user_by_id(db, tenant_id, user_id)
    if user is None:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="User not found."),
            status_code=303,
        )

    form = await request.form()
    first_name = _user_form_value(form, "first_name")
    last_name = _user_form_value(form, "last_name")
    email = normalize_email(form.get("email"))
    role = normalize_role(form.get("role"), default=None)
    is_active = _checkbox_checked(form, "is_active")

    if not validate_email(email):
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="A valid email address is required."),
            status_code=303,
        )
    if role not in TENANT_USER_ROLES:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Select a valid tenant role."),
            status_code=303,
        )
    if _existing_user_with_email(db, tenant_id=tenant_id, email=email, exclude_user_id=user.id) is not None:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="That email is already in use for this workspace."),
            status_code=303,
        )

    current_role = normalize_role(getattr(user, "role", None), default=None)
    current_is_active = bool(getattr(user, "is_active", False))
    removing_last_active_admin = (
        current_role == ROLE_TENANT_ADMIN
        and current_is_active
        and (role != ROLE_TENANT_ADMIN or not is_active)
        and _active_tenant_admin_count(db, tenant_id, exclude_user_id=user.id) == 0
    )
    if removing_last_active_admin:
        return RedirectResponse(
            url=_tenant_user_management_url(
                tenant_id,
                error="You must keep at least one active Tenant Admin in the workspace.",
            ),
            status_code=303,
        )

    identity_before = user_snapshot(user)
    set_user_identity_email(user, email)
    user.first_name = first_name or None
    user.last_name = last_name or None
    user.role = role
    user.is_active = is_active

    if current_role != role:
        audit_log(
            db,
            request,
            action="USER_ROLE_CHANGE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Changed role for {email}",
            details={"changed": {"role": {"from": current_role, "to": role}}},
            user=acting_user,
            tenant_id=tenant_id,
        )
    if current_is_active != is_active:
        audit_log(
            db,
            request,
            action="USER_ACTIVATE" if is_active else "USER_DEACTIVATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"{'Activated' if is_active else 'Deactivated'} user {email}",
            details={"changed": {"is_active": {"from": current_is_active, "to": is_active}}},
            user=acting_user,
            tenant_id=tenant_id,
        )
    identity_after = user_snapshot(user)
    identity_changes = audit_diff(
        identity_before,
        identity_after,
        ["username", "email", "first_name", "last_name"],
    )
    if identity_changes["changed"]:
        audit_log(
            db,
            request,
            action="USER_UPDATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Updated user details for {email}",
            details=identity_changes,
            user=acting_user,
            tenant_id=tenant_id,
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="That email is already in use for this workspace."),
            status_code=303,
        )

    return RedirectResponse(
        url=_tenant_user_management_url(tenant_id, message="User updated."),
        status_code=303,
    )


@router.post("/admin/users/{user_id:int}/reset-password", response_class=HTMLResponse)
async def admin_users_reset_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    acting_user = _require_tenant_user_admin(request)
    tenant_id = _current_tenant_id(request)
    user = _tenant_user_by_id(db, tenant_id, user_id)
    if user is None:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="User not found."),
            status_code=303,
        )

    form = await request.form()
    password = _user_form_value(form, "password")
    confirm_password = _user_form_value(form, "confirm_password")
    if len(password) < 8:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Password must be at least 8 characters."),
            status_code=303,
        )
    if password != confirm_password:
        return RedirectResponse(
            url=_tenant_user_management_url(tenant_id, error="Passwords do not match."),
            status_code=303,
        )

    user.password_hash = hash_password(password)
    audit_log(
        db,
        request,
        action="USER_PASSWORD_RESET",
        entity_type="user",
        entity_id=user.id,
        summary=f"Reset password for {normalize_email(getattr(user, 'username', ''))}",
        details={"changed": {"password": {"reset": True}}},
        user=acting_user,
        tenant_id=tenant_id,
    )
    db.commit()
    return RedirectResponse(
        url=_tenant_user_management_url(tenant_id, message="Password reset."),
        status_code=303,
    )
