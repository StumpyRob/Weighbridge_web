from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import diff as audit_diff
from ..audit import log as audit_log
from ..audit import user_snapshot
from ..auth import (
    hash_password,
    login_redirect_response,
    normalize_email,
    require_user,
    set_user_identity_email,
    user_display_name,
    user_role_label,
    validate_email,
    verify_password,
)
from ..config import settings
from ..constants import NAME_MAX
from ..db import get_db
from ..models import User, UserFeedback
from ..models.base import utcnow
from ..security import validate_no_html
from ..services.email_service import get_platform_email_settings, send_email
from ..services.feedback import (
    FEEDBACK_EMAIL_STATUS_FAILED,
    FEEDBACK_EMAIL_STATUS_PENDING,
    FEEDBACK_EMAIL_STATUS_SENT,
    FEEDBACK_KIND_LABELS,
    FEEDBACK_STATUS_NEW,
    feedback_display_title,
    normalize_feedback_kind,
)
from ..services.signatures import normalize_png_data_url, png_has_visible_ink
from ..tenancy import host_without_port
from ..templating import templates
from ..timezones import UK_TIMEZONE_LABEL, uk_now_from_utc

router = APIRouter()
_FEEDBACK_TITLE_MAX = 120
_FEEDBACK_MESSAGE_MAX = 4000


def _current_user_record(request: Request, db: Session) -> User | None:
    current_user = require_user(request)
    if isinstance(current_user, RedirectResponse):
        return None
    return (
        db.execute(
            select(User)
            .execution_options(skip_tenant_scope=True)
            .where(User.id == int(current_user.id))
            .limit(1)
        )
        .scalars()
        .first()
    )


def _saved_signature_default_signer_name(user: User | None) -> str:
    saved_signer_name = str(getattr(user, "saved_signature_signer_name", "") or "").strip()
    if saved_signer_name:
        return saved_signer_name[:NAME_MAX].strip()
    return user_display_name(user)[:NAME_MAX].strip()


def _default_feedback_redirect_path(request: Request) -> str:
    if bool(getattr(request.state, "platform_mode", False)):
        return "/platform/tenants"
    return "/"


def _sanitize_local_redirect_path(request: Request, raw_target: object) -> str:
    target = str(raw_target or "").strip()
    if not target:
        return _default_feedback_redirect_path(request)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return _default_feedback_redirect_path(request)
    path = str(parsed.path or "").strip() or "/"
    if not path.startswith("/") or path.startswith("//"):
        return _default_feedback_redirect_path(request)
    return urlunsplit(("", "", path, parsed.query, ""))


def _redirect_with_feedback_status(
    request: Request,
    *,
    source_path: object,
    feedback_sent: bool = False,
    feedback_kind: str = "",
    feedback_error: str = "",
) -> RedirectResponse:
    base_target = _sanitize_local_redirect_path(request, source_path)
    parsed = urlsplit(base_target)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"feedback_sent", "feedback_kind", "feedback_error"}
    ]
    if feedback_sent:
        query_items.append(("feedback_sent", "1"))
        if feedback_kind in FEEDBACK_KIND_LABELS:
            query_items.append(("feedback_kind", feedback_kind))
    elif feedback_error:
        query_items.append(("feedback_error", str(feedback_error).strip()))
    target = urlunsplit(
        (
            "",
            "",
            str(parsed.path or "").strip() or "/",
            urlencode(query_items),
            "",
        )
    )
    return RedirectResponse(url=target, status_code=303)


def _feedback_recipient(db: Session) -> str:
    configured = normalize_email(settings.effective_developer_feedback_email)
    if validate_email(configured):
        return configured

    transport = get_platform_email_settings(db)
    reply_to = normalize_email(transport.reply_to)
    if validate_email(reply_to):
        return reply_to

    from_email = normalize_email(transport.from_email)
    if validate_email(from_email):
        return from_email
    return ""


def _feedback_subject_title(title: str, source_title: str, source_path: str) -> str:
    clean_title = str(title or "").strip()
    if clean_title:
        return clean_title[:_FEEDBACK_TITLE_MAX].strip()
    clean_source_title = str(source_title or "").split("|", 1)[0].strip()
    if clean_source_title:
        return clean_source_title[:_FEEDBACK_TITLE_MAX].strip()
    return (str(source_path or "").strip() or "General feedback")[:_FEEDBACK_TITLE_MAX].strip()


def _feedback_workspace_label(request: Request) -> str:
    if bool(getattr(request.state, "platform_mode", False)):
        return "Platform Admin"
    tenant = getattr(request.state, "tenant", None)
    tenant_name = str(getattr(tenant, "name", "") or "").strip()
    if tenant_name:
        return tenant_name
    tenant_subdomain = str(getattr(tenant, "subdomain", "") or "").strip()
    if tenant_subdomain:
        return tenant_subdomain
    return "Workspace"


def _profile_form_data(
    user: User | None,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
) -> dict[str, str]:
    return {
        "first_name": str(
            first_name if first_name is not None else getattr(user, "first_name", "") or ""
        ).strip(),
        "last_name": str(
            last_name if last_name is not None else getattr(user, "last_name", "") or ""
        ).strip(),
        "email": str(email if email is not None else getattr(user, "email", "") or "").strip(),
    }


def _workspace_user_with_email(
    db: Session,
    *,
    user: User,
    email: str,
    exclude_user_id: int | None = None,
) -> User | None:
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None

    statement = (
        select(User)
        .execution_options(skip_tenant_scope=True)
        .where(func.lower(User.email) == normalized_email)
    )
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        statement = statement.where(User.tenant_id.is_(None))
    else:
        statement = statement.where(User.tenant_id == int(tenant_id))
    if exclude_user_id is not None:
        statement = statement.where(User.id != int(exclude_user_id))
    return db.execute(statement.limit(1)).scalars().first()


def _render_account_page(
    request: Request,
    user: User,
    *,
    profile_errors: list[str] | None = None,
    password_errors: list[str] | None = None,
    profile_form: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "account/profile.html",
        {
            "request": request,
            "user": user,
            "profile_form": profile_form or _profile_form_data(user),
            "profile_errors": list(profile_errors or []),
            "password_errors": list(password_errors or []),
            "profile_saved": request.query_params.get("profile_saved") == "1"
            and status_code < 400,
            "password_saved": request.query_params.get("password_saved") == "1"
            and status_code < 400,
            "active_account_tab": "account",
        },
        status_code=status_code,
    )


def _render_account_signature_page(
    request: Request,
    user: User,
    *,
    errors: list[str] | None = None,
    signer_name: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    dialog_signer_name = (
        str(signer_name or "").strip()
        or _saved_signature_default_signer_name(user)
    )
    return templates.TemplateResponse(
        request,
        "account/signature.html",
        {
            "request": request,
            "user": user,
            "errors": list(errors or []),
            "saved": request.query_params.get("saved") == "1" and status_code < 400,
            "dialog_signer_name": dialog_signer_name,
            "active_account_tab": "signature",
        },
        status_code=status_code,
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)
    return _render_account_page(request, user)


@router.get("/account/signature", response_class=HTMLResponse)
def account_signature_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)
    return _render_account_signature_page(request, user)


@router.post("/account/profile", response_class=HTMLResponse)
async def account_profile_save(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)

    form = await request.form()
    first_name = str(form.get("first_name") or "").strip()
    last_name = str(form.get("last_name") or "").strip()
    email = normalize_email(form.get("email"))
    current_password = str(form.get("current_password") or "")

    errors: list[str] = []
    validate_no_html(first_name, "First name", errors)
    validate_no_html(last_name, "Last name", errors)
    if len(first_name) > 100:
        errors.append("First name must be 100 characters or fewer.")
    if len(last_name) > 100:
        errors.append("Last name must be 100 characters or fewer.")
    if not validate_email(email):
        errors.append("A valid email address is required.")

    current_email = normalize_email(getattr(user, "email", None))
    email_changed = email != current_email
    if email_changed:
        if not current_password:
            errors.append("Current password is required to change your sign-in email.")
        elif not verify_password(current_password, getattr(user, "password_hash", None)):
            errors.append("Current password is incorrect.")
        elif (
            _workspace_user_with_email(
                db,
                user=user,
                email=email,
                exclude_user_id=int(user.id),
            )
            is not None
        ):
            errors.append("That email is already in use for this workspace.")

    profile_form = _profile_form_data(
        user,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )
    if errors:
        return _render_account_page(
            request,
            user,
            profile_errors=errors,
            profile_form=profile_form,
            status_code=400,
        )

    identity_before = user_snapshot(user)
    set_user_identity_email(user, email)
    user.first_name = first_name or None
    user.last_name = last_name or None
    identity_changes = audit_diff(
        identity_before,
        user_snapshot(user),
        ["username", "email", "first_name", "last_name"],
    )
    if identity_changes["changed"]:
        audit_log(
            db,
            request,
            action="USER_UPDATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Updated own account details for {email}",
            details=identity_changes,
            user=user,
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_account_page(
            request,
            user,
            profile_errors=["That email is already in use for this workspace."],
            profile_form=profile_form,
            status_code=400,
        )

    return RedirectResponse(url="/account?profile_saved=1", status_code=303)


@router.post("/account/password", response_class=HTMLResponse)
async def account_password_save(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)

    form = await request.form()
    current_password = str(form.get("current_password") or "")
    new_password = str(form.get("password") or "")
    confirm_password = str(form.get("confirm_password") or "")

    errors: list[str] = []
    if not verify_password(current_password, getattr(user, "password_hash", None)):
        errors.append("Current password is incorrect.")
    if len(new_password) < 8:
        errors.append("Password must be at least 8 characters.")
    if new_password != confirm_password:
        errors.append("Passwords do not match.")
    if verify_password(new_password, getattr(user, "password_hash", None)):
        errors.append("New password must be different from your current password.")

    if errors:
        return _render_account_page(
            request,
            user,
            password_errors=errors,
            status_code=400,
        )

    user.password_hash = hash_password(new_password)
    audit_log(
        db,
        request,
        action="USER_PASSWORD_CHANGE",
        entity_type="user",
        entity_id=user.id,
        summary="Changed own password",
        details={
            "changed": {
                "password": {
                    "reset": True,
                }
            },
            "self_service": True,
        },
        user=user,
    )
    db.commit()
    return RedirectResponse(url="/account?password_saved=1", status_code=303)


@router.post("/account/signature", response_class=HTMLResponse)
async def account_signature_save(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)

    form = await request.form()
    signature_data_url = str(form.get("signature_data_url") or "").strip()
    signer_name_input = str(form.get("signer_name") or "").strip()

    errors: list[str] = []
    signer_name: str | None = None
    if signer_name_input:
        validate_no_html(signer_name_input, "Signer name", errors)
        if len(signer_name_input) > NAME_MAX:
            errors.append(f"Signer name must be {NAME_MAX} characters or fewer.")
        signer_name = signer_name_input

    normalized_signature = normalize_png_data_url(signature_data_url)
    if normalized_signature is None:
        errors.append("Signature image is invalid. Please capture and save again.")
    else:
        normalized_data_url, normalized_png_bytes = normalized_signature
        if not png_has_visible_ink(normalized_png_bytes):
            errors.append("Signature cannot be blank.")

    if errors:
        return _render_account_signature_page(
            request,
            user,
            errors=errors,
            signer_name=signer_name_input,
            status_code=400,
        )

    assert normalized_signature is not None
    normalized_data_url, _normalized_png_bytes = normalized_signature
    replacing_existing = user.has_saved_signature
    updated_at = utcnow()
    user.saved_signature_data_uri = normalized_data_url
    user.saved_signature_signer_name = signer_name
    user.saved_signature_updated_at = updated_at
    audit_log(
        db,
        request,
        action="USER_SIGNATURE_UPDATED" if replacing_existing else "USER_SIGNATURE_SAVED",
        entity_type="user",
        entity_id=user.id,
        summary=(
            "Updated saved receiver signature"
            if replacing_existing
            else "Saved receiver signature"
        ),
        details={
            "signer_name": signer_name or None,
            "updated_at": updated_at.isoformat(),
            "operation": "replace" if replacing_existing else "save",
        },
    )
    db.add(user)
    db.commit()
    return RedirectResponse(url="/account/signature?saved=1", status_code=303)


@router.post("/feedback")
async def feedback_submit(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)

    form = await request.form()
    feedback_kind = normalize_feedback_kind(form.get("kind"), default="bug") or "bug"
    title = str(form.get("title") or "").strip()
    message = str(form.get("message") or "").strip()
    source_path = str(form.get("source_path") or "").strip()
    source_title = str(form.get("source_title") or "").strip()

    if feedback_kind not in FEEDBACK_KIND_LABELS:
        return _redirect_with_feedback_status(
            request,
            source_path=source_path,
            feedback_error="Choose bug report or feature request.",
        )

    errors: list[str] = []
    validate_no_html(title, "Title", errors)
    validate_no_html(message, "Message", errors)
    validate_no_html(source_title, "Page title", errors)
    if len(title) > _FEEDBACK_TITLE_MAX:
        errors.append(f"Title must be {_FEEDBACK_TITLE_MAX} characters or fewer.")
    if not message:
        errors.append("Message is required.")
    elif len(message) > _FEEDBACK_MESSAGE_MAX:
        errors.append(f"Message must be {_FEEDBACK_MESSAGE_MAX} characters or fewer.")

    sanitized_source_path = _sanitize_local_redirect_path(request, source_path)
    normalized_source_title = source_title[:255].strip() or None
    if errors:
        return _redirect_with_feedback_status(
            request,
            source_path=sanitized_source_path,
            feedback_error=errors[0],
        )

    workspace_label = _feedback_workspace_label(request)
    host_label = host_without_port(str(request.url.hostname or "")).strip()
    user_email = str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip()
    feedback_to = _feedback_recipient(db)
    feedback = UserFeedback(
        submitted_by_user_id=int(user.id),
        kind=feedback_kind,
        status=FEEDBACK_STATUS_NEW,
        title=title[:_FEEDBACK_TITLE_MAX].strip() or None,
        message=message,
        source_path=sanitized_source_path[:255].strip() or None,
        source_title=normalized_source_title,
        submitted_by_display_name=user_display_name(user)[:NAME_MAX].strip() or None,
        submitted_by_email=user_email[:255].strip() or None,
        host_name=host_label[:255].strip() or None,
        recipient_email=feedback_to[:255].strip() or None if feedback_to else None,
        email_delivery_status=(
            FEEDBACK_EMAIL_STATUS_PENDING
            if feedback_to
            else FEEDBACK_EMAIL_STATUS_FAILED
        ),
        email_delivery_error=(
            None if feedback_to else "Support email is not configured."
        ),
    )
    db.add(feedback)
    db.flush()

    feedback_label = FEEDBACK_KIND_LABELS[feedback_kind]
    subject_title = _feedback_subject_title(title, normalized_source_title or "", sanitized_source_path)
    subject = f"[{feedback_label}] {workspace_label}: #{int(feedback.id)} {subject_title}"
    submitted_at = uk_now_from_utc(utcnow()).strftime("%d/%m/%Y %H:%M:%S")
    body_lines = [
        f"{feedback_label} from Weighbridge Web",
        "",
        f"Feedback ID: {int(feedback.id)}",
        f"Workspace: {workspace_label}",
        f"User: {user_display_name(user)}",
        f"Email: {user_email or '-'}",
        f"Role: {user_role_label(user)}",
        f"Page: {sanitized_source_path}",
        f"Host: {host_label or '-'}",
        f"Submitted: {submitted_at} ({UK_TIMEZONE_LABEL})",
    ]
    if normalized_source_title:
        body_lines.append(f"Page title: {normalized_source_title}")
    if title:
        body_lines.append(f"Title: {title}")
    body_lines.extend(
        [
            "",
            "Message:",
            message,
        ]
    )
    if feedback_to:
        result = send_email(
            subject=subject,
            text_body="\n".join(body_lines).strip(),
            to=[feedback_to],
            db=db,
        )
        feedback.email_delivery_status = (
            FEEDBACK_EMAIL_STATUS_SENT if result.ok else FEEDBACK_EMAIL_STATUS_FAILED
        )
        feedback.email_delivery_error = (
            None if result.ok else (result.error or "Feedback send failed.")
        )
    else:
        class _LocalResult:
            ok = False
            error = "Saved to the workspace inbox, but support email is not configured."

        result = _LocalResult()
    audit_log(
        db,
        request,
        action="USER_FEEDBACK_SUBMIT",
        entity_type="user_feedback",
        entity_id=feedback.id,
        summary=(
            f"{feedback_label} submitted from {sanitized_source_path}"
            if result.ok
            else f"{feedback_label} email failed from {sanitized_source_path}"
        ),
        details={
            "feedback_id": int(feedback.id),
            "submitted_by_user_id": int(user.id),
            "kind": feedback_kind,
            "title": feedback.title,
            "display_title": feedback_display_title(feedback),
            "source_title": normalized_source_title,
            "source_path": sanitized_source_path,
            "workspace": workspace_label,
            "recipient": feedback_to,
            "status": "sent" if result.ok else "failed",
            "error": feedback.email_delivery_error,
        },
        user=user,
    )
    db.commit()
    if result.ok:
        return _redirect_with_feedback_status(
            request,
            source_path=sanitized_source_path,
            feedback_sent=True,
            feedback_kind=feedback_kind,
        )
    return _redirect_with_feedback_status(
        request,
        source_path=sanitized_source_path,
        feedback_error=result.error or "Saved to the workspace inbox, but feedback email failed.",
    )
