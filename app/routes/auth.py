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
    SESSION_USER_ID_KEY,
    hash_password,
    normalize_email,
    user_identity_kwargs,
    user_by_email,
    validate_email,
    verify_password,
)
from ..db import get_db
from ..models import User
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


def _login_context(
    request: Request,
    *,
    errors: list[str] | None = None,
    email: str = "",
    next_path: str = "/tickets",
    no_users_configured: bool = False,
) -> dict[str, object]:
    return {
        "request": request,
        "errors": errors or [],
        "email": email,
        "next": next_path,
        "logged_out": request.query_params.get("logged_out") == "1",
        "bootstrapped": request.query_params.get("bootstrap") == "1",
        "no_users_configured": no_users_configured,
    }


def _user_count(db: Session) -> int:
    return int(db.execute(select(func.count(User.id))).scalar_one_or_none() or 0)


def _bootstrap_enabled(db: Session) -> bool:
    return _user_count(db) == 0


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
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current = getattr(request.state, "current_user", None)
    if isinstance(current, User) and current.is_active:
        return RedirectResponse(url="/tickets", status_code=303)

    user_count = _user_count(db)
    next_path = _safe_next_path(request.query_params.get("next"))
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        _login_context(
            request,
            next_path=next_path,
            no_users_configured=user_count == 0,
        ),
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current = getattr(request.state, "current_user", None)
    if isinstance(current, User) and current.is_active:
        return RedirectResponse(url="/tickets", status_code=303)

    form = await request.form()
    email = normalize_email(str(form.get("email", "")))
    password = str(form.get("password", ""))
    next_path = _safe_next_path(form.get("next"))

    errors: list[str] = []
    if not validate_email(email):
        errors.append("Enter a valid email address.")
    if not password:
        errors.append("Password is required.")

    if errors:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            _login_context(request, errors=errors, email=email, next_path=next_path),
            status_code=400,
        )

    user = user_by_email(db, email)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
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
            ),
            status_code=401,
        )

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
    return RedirectResponse(url=next_path, status_code=303)


@router.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _bootstrap_enabled(db):
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(
        request,
        "auth/bootstrap.html",
        _bootstrap_context(request),
    )


@router.post("/bootstrap", response_class=HTMLResponse)
async def bootstrap_submit(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if not _bootstrap_enabled(db):
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
    return RedirectResponse(url="/login?bootstrap=1", status_code=303)


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
