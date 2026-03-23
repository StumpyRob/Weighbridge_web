from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..auth import login_redirect_response, require_user, user_display_name
from ..constants import NAME_MAX
from ..db import get_db
from ..models import User
from ..models.base import utcnow
from ..security import validate_no_html
from ..services.signatures import normalize_png_data_url, png_has_visible_ink
from ..templating import templates

router = APIRouter()


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
        },
        status_code=status_code,
    )


@router.get("/account/signature", response_class=HTMLResponse)
def account_signature_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = _current_user_record(request, db)
    if user is None or not bool(getattr(user, "is_active", False)):
        return login_redirect_response(request)
    return _render_account_signature_page(request, user)


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
