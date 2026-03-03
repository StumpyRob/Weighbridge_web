from __future__ import annotations

import re
from urllib.parse import quote

import bcrypt
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import User

ROLE_SUPERADMIN = "SUPERADMIN"
ROLE_ADMIN = "ADMIN"
ROLE_OPERATOR = "OPERATOR"

SESSION_USER_ID_KEY = "user_id"

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


def user_by_email(db: Session, email: str | None) -> User | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    email_col = _user_email_column()
    return (
        db.execute(select(User).where(func.lower(email_col) == normalized))
        .scalars()
        .first()
    )


def user_identity_kwargs(*, email: str, role: str | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    normalized = normalize_email(email)
    if hasattr(User, "email"):
        kwargs["email"] = normalized
    if hasattr(User, "username"):
        kwargs["username"] = normalized
    if role and hasattr(User, "role"):
        kwargs["role"] = role
    return kwargs


def is_superadmin_user(db: Session, user: User | None) -> bool:
    if user is None or not bool(getattr(user, "is_active", False)):
        return False

    role = str(getattr(user, "role", "") or "").strip().upper()
    if role:
        return role == ROLE_SUPERADMIN

    first_user_id = db.execute(select(func.min(User.id))).scalar_one_or_none()
    return first_user_id is not None and int(first_user_id) == int(user.id)


def is_admin_user(db: Session, user: User | None) -> bool:
    if user is None or not bool(getattr(user, "is_active", False)):
        return False
    role = str(getattr(user, "role", "") or "").strip().upper()
    if role:
        return role in {ROLE_SUPERADMIN, ROLE_ADMIN}
    return is_superadmin_user(db, user)


def user_display_name(user: User | None) -> str:
    if user is None:
        return "System"
    email = str(getattr(user, "email", "") or "").strip()
    if email:
        return email
    username = str(getattr(user, "username", "") or "").strip()
    if username:
        return username
    return f"User {getattr(user, 'id', '?')}"


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
