from __future__ import annotations

import re
from urllib.parse import quote

import bcrypt
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import User
from .user_roles import (
    ROLE_ACCOUNTS as _ROLE_ACCOUNTS,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_OPERATOR as _ROLE_OPERATOR,
    ROLE_READ_ONLY as _ROLE_READ_ONLY,
    ROLE_USER,
    legacy_role_for_tenant_id,
    normalize_role,
    role_label,
)

ROLE_ADMIN = ROLE_TENANT_ADMIN
ROLE_OPERATOR = _ROLE_OPERATOR
ROLE_ACCOUNTS = _ROLE_ACCOUNTS
ROLE_READ_ONLY = _ROLE_READ_ONLY

SESSION_USER_ID_KEY = "user_id"
SESSION_TENANT_ID_KEY = "tenant_id"
SESSION_ROLE_KEY = "role"
SESSION_PLATFORM_MODE_KEY = "platform_mode"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def validate_email(email: str | None) -> bool:
    candidate = normalize_email(email)
    if not candidate:
        return False
    return bool(_EMAIL_RE.fullmatch(candidate))


def hash_password(password: str) -> str:
    payload = str(password or "").encode("utf-8")
    hashed = bcrypt.hashpw(payload, bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    incoming = str(password or "").encode("utf-8")
    stored = str(password_hash or "").encode("utf-8")
    if not incoming or not stored:
        return False
    try:
        return bool(bcrypt.checkpw(incoming, stored))
    except ValueError:
        return False


def _user_email_column():
    # Current schema may store identity as username; keep compatibility.
    email_col = getattr(User, "email", None)
    if email_col is not None:
        return email_col
    return getattr(User, "username")


def user_by_email(
    db: Session,
    email: str | None,
    *,
    skip_tenant_scope: bool = False,
) -> User | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    email_col = _user_email_column()
    statement = select(User).where(func.lower(email_col) == normalized)
    if skip_tenant_scope:
        statement = statement.execution_options(skip_tenant_scope=True)
    return db.execute(statement).scalars().first()


def user_identity_kwargs(*, email: str, role: str | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    normalized = normalize_email(email)
    if hasattr(User, "email"):
        kwargs["email"] = normalized
    if hasattr(User, "username"):
        kwargs["username"] = normalized
    if role and hasattr(User, "role"):
        kwargs["role"] = normalize_role(role, default=ROLE_OPERATOR)
    return kwargs


def set_user_identity_email(user: User, email: str) -> None:
    normalized = normalize_email(email)
    if hasattr(User, "email"):
        setattr(user, "email", normalized)
    if hasattr(User, "username"):
        setattr(user, "username", normalized)


def _all_users_statement(statement):
    return statement.execution_options(skip_tenant_scope=True)


def _platform_superadmin_count(db: Session) -> int:
    return int(
        db.execute(
            _all_users_statement(
                select(func.count(User.id)).where(
                    User.tenant_id.is_(None),
                    func.lower(func.trim(User.role)) == ROLE_SUPERADMIN,
                )
            )
        ).scalar_one_or_none()
        or 0
    )


def _first_platform_user_id(db: Session) -> int | None:
    value = db.execute(
        _all_users_statement(
            select(func.min(User.id)).where(User.tenant_id.is_(None))
        )
    ).scalar_one_or_none()
    if value is None:
        return None
    return int(value)


def ensure_user_role(
    db: Session,
    user: User | None,
    *,
    allow_bootstrap: bool = True,
) -> str:
    if user is None:
        return ROLE_USER

    stored_role = getattr(user, "role", None)
    tenant_id = getattr(user, "tenant_id", None)
    normalized = normalize_role(stored_role, default=None)
    changed = False

    if normalized is None:
        normalized = legacy_role_for_tenant_id(tenant_id)
        changed = True
    elif str(stored_role or "").strip().lower() != normalized:
        changed = True

    user_id = getattr(user, "id", None)
    if (
        allow_bootstrap
        and tenant_id is None
        and normalized != ROLE_SUPERADMIN
        and user_id is not None
        and _platform_superadmin_count(db) == 0
    ):
        first_platform_user_id = _first_platform_user_id(db)
        if first_platform_user_id is not None and int(first_platform_user_id) == int(user_id):
            normalized = ROLE_SUPERADMIN
            changed = True

    if normalized == ROLE_SUPERADMIN and tenant_id is not None:
        user.tenant_id = None
        changed = True

    if str(getattr(user, "role", "") or "").strip() != normalized:
        user.role = normalized
        changed = True

    if changed:
        db.flush()
    return normalized


def is_superadmin_user(db: Session, user: User | None) -> bool:
    if user is None or not bool(getattr(user, "is_active", False)):
        return False

    role = ensure_user_role(db, user, allow_bootstrap=True)
    return role == ROLE_SUPERADMIN and getattr(user, "tenant_id", None) is None


def is_admin_user(db: Session, user: User | None) -> bool:
    if user is None or not bool(getattr(user, "is_active", False)):
        return False
    role = ensure_user_role(db, user, allow_bootstrap=True)
    return role in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN}


def user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    first_name = str(getattr(user, "first_name", "") or "").strip()
    last_name = str(getattr(user, "last_name", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return full_name
    email = str(getattr(user, "email", "") or "").strip()
    if email:
        return email
    username = str(getattr(user, "username", "") or "").strip()
    if username:
        return username
    return f"User {getattr(user, 'id', '?')}"


def user_role_label(user: User | None) -> str:
    if user is None:
        return "Unknown"
    return role_label(getattr(user, "role", None))


def login_next_path(request: Request) -> str:
    path = str(request.url.path or "").strip() or "/"
    query = str(request.url.query or "").strip()
    if query:
        return f"{path}?{query}"
    return path


def login_redirect_response(request: Request, *, status_code: int = 302) -> RedirectResponse:
    next_path = login_next_path(request)
    encoded_next = quote(next_path, safe="/?=&")
    return RedirectResponse(
        url=f"/login?next={encoded_next}",
        status_code=status_code,
    )


def require_user(request: Request) -> User | RedirectResponse:
    current = getattr(getattr(request, "state", None), "current_user", None)
    if isinstance(current, User) and bool(current.is_active):
        return current
    return login_redirect_response(request)
