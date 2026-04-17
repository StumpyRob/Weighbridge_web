from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import log as audit_log
from ..auth import ensure_user_role
from ..config import settings
from ..db import get_db
from ..models import (
    AccountingConnection,
    AccountingSyncEvent,
    AccountingSyncJob,
    Tenant,
    User,
)
from ..models.base import utcnow
from ..permissions import PERM_MANAGE_SETTINGS, require_permission
from ..services.accounting.job_runner import process_pending_accounting_jobs
from ..services.accounting.quickbooks_oauth import (
    QUICKBOOKS_CALLBACK_PATH,
    QUICKBOOKS_PROVIDER,
    QuickBooksOAuthError,
    build_quickbooks_authorize_url,
    disconnect_quickbooks_connection,
    exchange_code_for_tokens,
    get_or_create_quickbooks_connection,
    resolve_quickbooks_redirect_uri,
    store_quickbooks_tokens,
)
from ..services.accounting.tax_mapping import (
    TaxMappingValidationError,
    create_quickbooks_tax_mapping,
    delete_quickbooks_tax_mapping,
    summarize_quickbooks_setup,
    update_quickbooks_tax_mapping,
)
from ..templating import templates
from ..tenancy import request_platform_mode, request_tenant_id, tenant_request_url
from ..user_roles import ROLE_TENANT_ADMIN

router = APIRouter()
_OAUTH_STATE_TTL = timedelta(minutes=10)


def _require_tenant_accounting_admin(request: Request, db: Session) -> User:
    if request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    user = require_permission(request, PERM_MANAGE_SETTINGS)
    role = ensure_user_role(db, user, allow_bootstrap=True)
    if role != ROLE_TENANT_ADMIN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _quickbooks_connection(
    db: Session,
    *,
    tenant_id: int,
) -> AccountingConnection | None:
    return (
        db.execute(
            select(AccountingConnection).where(
                AccountingConnection.tenant_id == int(tenant_id),
                AccountingConnection.provider == QUICKBOOKS_PROVIDER,
            )
        )
        .scalars()
        .first()
    )


def _quickbooks_state_secret() -> bytes:
    secret = str(settings.effective_secret_key or "").strip()
    if not secret:
        raise QuickBooksOAuthError("APP_SECRET_KEY (or SECRET_KEY) is not configured.")
    return secret.encode("utf-8")


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(token: str) -> bytes:
    raw = str(token or "").strip()
    if not raw:
        raise ValueError("missing token")
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii"))


def _signed_quickbooks_state(payload: dict[str, object]) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_token = _urlsafe_b64encode(payload_json)
    signature = hmac.new(
        _quickbooks_state_secret(),
        payload_token.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_token}.{_urlsafe_b64encode(signature)}"


def _validated_quickbooks_state(state: str) -> dict[str, object]:
    raw_state = str(state or "").strip()
    if not raw_state or "." not in raw_state:
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.")
    payload_token, signature_token = raw_state.split(".", 1)
    expected_signature = hmac.new(
        _quickbooks_state_secret(),
        payload_token.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided_signature = _urlsafe_b64decode(signature_token)
        payload = json.loads(_urlsafe_b64decode(payload_token))
    except (ValueError, json.JSONDecodeError) as exc:
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.") from exc
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.")
    if not isinstance(payload, dict):
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.")
    issued_at_raw = payload.get("issued_at")
    return_url = str(payload.get("return_url", "") or "").strip()
    try:
        issued_at = datetime.fromisoformat(str(issued_at_raw or "").strip())
        tenant_id = int(payload.get("tenant_id") or 0)
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError) as exc:
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.") from exc
    if issued_at < utcnow() - _OAUTH_STATE_TTL:
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.")
    if tenant_id <= 0 or user_id <= 0 or not return_url:
        raise QuickBooksOAuthError("QuickBooks callback could not be verified.")
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "return_url": return_url,
        "issued_at": issued_at.isoformat(),
    }


def _issue_quickbooks_state(
    request: Request,
    *,
    tenant_id: int,
    user_id: int,
) -> str:
    return _signed_quickbooks_state(
        {
            "tenant_id": int(tenant_id),
            "user_id": int(user_id),
            "return_url": tenant_request_url(request, path="/admin/accounting"),
            "issued_at": utcnow().isoformat(),
        }
    )


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(str(url or "").strip())
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value is not None})
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode(query),
            parsed.fragment,
        )
    )


def _quickbooks_callback_redirect(
    state_payload: dict[str, object],
    **params: str,
) -> RedirectResponse:
    return RedirectResponse(
        url=_append_query_params(str(state_payload.get("return_url") or ""), params),
        status_code=303,
    )


def _validated_quickbooks_callback_actor(
    db: Session,
    *,
    state_payload: dict[str, object],
) -> tuple[Tenant | None, User | None]:
    tenant_id = int(state_payload["tenant_id"])
    user_id = int(state_payload["user_id"])
    tenant = db.get(Tenant, tenant_id)
    user = db.get(User, user_id)
    if tenant is None or not bool(getattr(tenant, "is_active", False)):
        return tenant, None
    if user is None or not bool(getattr(user, "is_active", False)):
        return tenant, None
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        return tenant, None
    if ensure_user_role(db, user, allow_bootstrap=True) != ROLE_TENANT_ADMIN:
        return tenant, None
    return tenant, user


def _record_accounting_event(
    db: Session,
    *,
    tenant_id: int,
    event_type: str,
    direction: str,
    summary: str,
    entity_id: int | None = None,
    detail_json: dict[str, object] | None = None,
) -> None:
    db.add(
        AccountingSyncEvent(
            tenant_id=int(tenant_id),
            provider=QUICKBOOKS_PROVIDER,
            event_type=event_type,
            entity_type="accounting_connection" if entity_id is not None else None,
            entity_id=int(entity_id) if entity_id is not None else None,
            direction=direction,
            summary=summary,
            detail_json=detail_json,
        )
    )


def _apply_connection_error(
    connection: AccountingConnection | None,
    *,
    message: str,
) -> None:
    if connection is None:
        return
    if str(connection.status or "").strip().lower() != "connected":
        connection.status = "error"
    connection.last_error = str(message or "").strip() or None
    connection.updated_at = utcnow()


def _query_int(request: Request, key: str) -> int:
    raw = str(request.query_params.get(key, "") or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _query_flag(request: Request, key: str) -> bool:
    return str(request.query_params.get(key, "") or "").strip() == "1"


def _form_flag(form: object, key: str) -> bool:
    raw = str(getattr(form, "get", lambda _key, _default=None: None)(key, "") or "").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def _form_int(form: object, key: str, *, label: str) -> int:
    raw = str(getattr(form, "get", lambda _key, _default=None: None)(key, "") or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise TaxMappingValidationError(f"{label} is required.")


def _tax_mappings_redirect(
    *,
    saved: bool = False,
    deleted: bool = False,
    error: str = "",
) -> RedirectResponse:
    query: dict[str, str] = {}
    if saved:
        query["tax_mapping_saved"] = "1"
    if deleted:
        query["tax_mapping_deleted"] = "1"
    if error:
        query["error"] = error
    url = "/admin/accounting/tax-mappings"
    if query:
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url=url, status_code=303)


def _page_context(
    request: Request,
    *,
    connection: AccountingConnection | None,
    recent_jobs: list[AccountingSyncJob] | None = None,
    recent_events: list[AccountingSyncEvent] | None = None,
    setup_summary: object | None = None,
    config_error: str = "",
) -> dict[str, object]:
    return {
        "request": request,
        "connection": connection,
        "recent_jobs": recent_jobs or [],
        "recent_events": recent_events or [],
        "config_error": config_error,
        "quickbooks_connected": _query_flag(request, "quickbooks_connected"),
        "quickbooks_disconnected": _query_flag(request, "quickbooks_disconnected"),
        "tax_mapping_saved": _query_flag(request, "tax_mapping_saved"),
        "tax_mapping_deleted": _query_flag(request, "tax_mapping_deleted"),
        "sync_run": _query_flag(request, "sync_run"),
        "sync_retry_run": _query_flag(request, "sync_retry_run"),
        "sync_processed": _query_int(request, "sync_processed"),
        "sync_succeeded": _query_int(request, "sync_succeeded"),
        "sync_failed": _query_int(request, "sync_failed"),
        "setup_summary": setup_summary,
        "error": str(request.query_params.get("error", "") or "").strip(),
    }


@router.get("/admin/accounting", response_class=HTMLResponse)
def admin_accounting(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    connection = _quickbooks_connection(db, tenant_id=tenant_id)
    setup_summary = summarize_quickbooks_setup(
        db,
        tenant_id=tenant_id,
        connection_status=getattr(connection, "status", None),
    )
    recent_jobs = (
        db.execute(
            select(AccountingSyncJob)
            .where(
                AccountingSyncJob.tenant_id == int(tenant_id),
                AccountingSyncJob.provider == QUICKBOOKS_PROVIDER,
            )
            .order_by(AccountingSyncJob.created_at.desc(), AccountingSyncJob.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    recent_events = (
        db.execute(
            select(AccountingSyncEvent)
            .where(
                AccountingSyncEvent.tenant_id == int(tenant_id),
                AccountingSyncEvent.provider == QUICKBOOKS_PROVIDER,
            )
            .order_by(AccountingSyncEvent.created_at.desc(), AccountingSyncEvent.id.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    config_error = ""
    try:
        redirect_uri = resolve_quickbooks_redirect_uri(request)
        if not str(settings.quickbooks_client_secret or "").strip():
            raise QuickBooksOAuthError("QUICKBOOKS_CLIENT_SECRET is not configured.")
        build_quickbooks_authorize_url(state="preview", redirect_uri=redirect_uri)
    except QuickBooksOAuthError as exc:
        config_error = str(exc)
    return templates.TemplateResponse(
        request,
        "admin/accounting/index.html",
        _page_context(
            request,
            connection=connection,
            recent_jobs=recent_jobs,
            recent_events=recent_events,
            setup_summary=setup_summary,
            config_error=config_error,
        ),
    )


@router.get("/admin/accounting/tax-mappings", response_class=HTMLResponse)
def admin_accounting_tax_mappings(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    connection = _quickbooks_connection(db, tenant_id=tenant_id)
    setup_summary = summarize_quickbooks_setup(
        db,
        tenant_id=tenant_id,
        connection_status=getattr(connection, "status", None),
    )
    return templates.TemplateResponse(
        request,
        "admin/accounting/tax_mappings.html",
        _page_context(
            request,
            connection=connection,
            setup_summary=setup_summary,
        ),
    )


@router.post("/admin/accounting/tax-mappings")
async def admin_accounting_tax_mappings_create(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    form = await request.form()
    try:
        create_quickbooks_tax_mapping(
            db,
            tenant_id=tenant_id,
            tax_rate_id=_form_int(form, "tax_rate_id", label="Local tax rate"),
            external_id=str(form.get("external_id", "") or "").strip() or None,
            external_code=str(form.get("external_code", "") or "").strip() or None,
            name=str(form.get("name", "") or "").strip() or None,
            is_active=_form_flag(form, "is_active"),
        )
        db.commit()
    except TaxMappingValidationError as exc:
        db.rollback()
        return _tax_mappings_redirect(error=str(exc))
    return _tax_mappings_redirect(saved=True)


@router.post("/admin/accounting/tax-mappings/{mapping_id}/update")
async def admin_accounting_tax_mappings_update(
    mapping_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    form = await request.form()
    try:
        update_quickbooks_tax_mapping(
            db,
            tenant_id=tenant_id,
            mapping_id=int(mapping_id),
            external_id=str(form.get("external_id", "") or "").strip() or None,
            external_code=str(form.get("external_code", "") or "").strip() or None,
            name=str(form.get("name", "") or "").strip() or None,
            is_active=_form_flag(form, "is_active"),
        )
        db.commit()
    except TaxMappingValidationError as exc:
        db.rollback()
        return _tax_mappings_redirect(error=str(exc))
    return _tax_mappings_redirect(saved=True)


@router.post("/admin/accounting/tax-mappings/{mapping_id}/delete")
def admin_accounting_tax_mappings_delete(
    mapping_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    try:
        delete_quickbooks_tax_mapping(
            db,
            tenant_id=tenant_id,
            mapping_id=int(mapping_id),
        )
        db.commit()
    except TaxMappingValidationError as exc:
        db.rollback()
        return _tax_mappings_redirect(error=str(exc))
    return _tax_mappings_redirect(deleted=True)


@router.get("/admin/accounting/quickbooks/connect")
def admin_accounting_quickbooks_connect(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    user_id = int(current_user.id)
    try:
        redirect_uri = resolve_quickbooks_redirect_uri(request)
        state = _issue_quickbooks_state(
            request,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        authorize_url = build_quickbooks_authorize_url(
            state=state,
            redirect_uri=redirect_uri,
        )
    except QuickBooksOAuthError as exc:
        error_text = str(exc)
        audit_log(
            db,
            request,
            action="ACCOUNTING_CONNECT_START_FAILED",
            entity_type="accounting_connection",
            summary="QuickBooks connect could not be started",
            details={
                "provider": QUICKBOOKS_PROVIDER,
                "error": error_text,
            },
        )
        _record_accounting_event(
            db,
            tenant_id=tenant_id,
            event_type="oauth_connect_start_failed",
            direction="OUTBOUND",
            summary="QuickBooks connect could not be started",
            detail_json={"error": error_text},
        )
        db.commit()
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': error_text})}",
            status_code=303,
        )

    audit_log(
        db,
        request,
        action="ACCOUNTING_CONNECT_START",
        entity_type="accounting_connection",
        summary="Started QuickBooks OAuth connect",
        details={"provider": QUICKBOOKS_PROVIDER},
    )
    _record_accounting_event(
        db,
        tenant_id=tenant_id,
        event_type="oauth_connect_started",
        direction="OUTBOUND",
        summary="Started QuickBooks OAuth connect",
        detail_json={"provider": QUICKBOOKS_PROVIDER},
    )
    db.commit()
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get(QUICKBOOKS_CALLBACK_PATH)
def admin_accounting_quickbooks_callback(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    if not request_platform_mode(request):
        raise HTTPException(status_code=404, detail="Not Found")
    state = str(request.query_params.get("state", "") or "").strip()
    code = str(request.query_params.get("code", "") or "").strip()
    realm_id = str(request.query_params.get("realmId", "") or "").strip() or None
    provider_error = str(request.query_params.get("error", "") or "").strip()
    provider_error_description = str(
        request.query_params.get("error_description", "") or ""
    ).strip()

    try:
        state_payload = _validated_quickbooks_state(state)
    except QuickBooksOAuthError as exc:
        error_text = str(exc)
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            summary="QuickBooks callback failed state validation",
            details={"provider": QUICKBOOKS_PROVIDER, "error": error_text},
        )
        db.commit()
        return PlainTextResponse(
            error_text,
            status_code=400,
        )

    tenant_id = int(state_payload["tenant_id"])
    _tenant, current_user = _validated_quickbooks_callback_actor(
        db,
        state_payload=state_payload,
    )
    if _tenant is None or current_user is None:
        error_text = "QuickBooks callback could not be verified."
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            summary="QuickBooks callback failed tenant or user validation",
            details={"provider": QUICKBOOKS_PROVIDER, "error": error_text},
            tenant_id=tenant_id,
        )
        db.commit()
        return _quickbooks_callback_redirect(state_payload, error=error_text)

    connection = _quickbooks_connection(db, tenant_id=tenant_id)

    if provider_error:
        error_text = provider_error_description or provider_error
        _apply_connection_error(connection, message=error_text)
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            entity_id=getattr(connection, "id", None),
            summary="QuickBooks callback returned an error",
            details={
                "provider": QUICKBOOKS_PROVIDER,
                "error": provider_error,
                "error_description": provider_error_description,
            },
            tenant_id=tenant_id,
            user=current_user,
        )
        _record_accounting_event(
            db,
            tenant_id=tenant_id,
            event_type="oauth_callback_failed",
            direction="INBOUND",
            summary="QuickBooks callback returned an error",
            entity_id=getattr(connection, "id", None),
            detail_json={
                "error": provider_error,
                "error_description": provider_error_description,
            },
        )
        db.commit()
        return _quickbooks_callback_redirect(
            state_payload,
            error=error_text or "QuickBooks callback failed.",
        )

    if not code:
        error_text = "QuickBooks authorization code is missing."
        _apply_connection_error(connection, message=error_text)
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            entity_id=getattr(connection, "id", None),
            summary="QuickBooks callback was missing an authorization code",
            details={"provider": QUICKBOOKS_PROVIDER, "error": error_text},
            tenant_id=tenant_id,
            user=current_user,
        )
        _record_accounting_event(
            db,
            tenant_id=tenant_id,
            event_type="oauth_callback_failed",
            direction="INBOUND",
            summary="QuickBooks callback was missing an authorization code",
            entity_id=getattr(connection, "id", None),
            detail_json={"error": error_text},
        )
        db.commit()
        return _quickbooks_callback_redirect(state_payload, error=error_text)

    try:
        redirect_uri = resolve_quickbooks_redirect_uri(request)
        token_bundle = exchange_code_for_tokens(
            code=code,
            redirect_uri=redirect_uri,
            realm_id=realm_id,
        )
    except QuickBooksOAuthError as exc:
        error_text = str(exc)
        connection = connection or get_or_create_quickbooks_connection(
            db,
            tenant_id=tenant_id,
        )
        _apply_connection_error(connection, message=error_text)
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            entity_id=connection.id,
            summary="QuickBooks token exchange failed",
            details={"provider": QUICKBOOKS_PROVIDER, "error": error_text},
            tenant_id=tenant_id,
            user=current_user,
        )
        _record_accounting_event(
            db,
            tenant_id=tenant_id,
            event_type="oauth_callback_failed",
            direction="INBOUND",
            summary="QuickBooks token exchange failed",
            entity_id=connection.id,
            detail_json={"error": error_text},
        )
        db.commit()
        return _quickbooks_callback_redirect(state_payload, error=error_text)

    connection = connection or get_or_create_quickbooks_connection(
        db,
        tenant_id=tenant_id,
    )
    store_quickbooks_tokens(connection, token_bundle=token_bundle)
    audit_log(
        db,
        request,
        action="ACCOUNTING_CONNECTED",
        entity_type="accounting_connection",
        entity_id=connection.id,
        summary="Connected QuickBooks company",
        details={
            "provider": QUICKBOOKS_PROVIDER,
            "realm_id": token_bundle.realm_id,
            "scopes": token_bundle.scopes,
        },
        tenant_id=tenant_id,
        user=current_user,
    )
    _record_accounting_event(
        db,
        tenant_id=tenant_id,
        event_type="oauth_connected",
        direction="INBOUND",
        summary="Connected QuickBooks company",
        entity_id=connection.id,
        detail_json={
            "realm_id": token_bundle.realm_id,
            "scopes": token_bundle.scopes,
        },
    )
    db.commit()
    return _quickbooks_callback_redirect(state_payload, quickbooks_connected="1")


@router.post("/admin/accounting/quickbooks/disconnect")
def admin_accounting_quickbooks_disconnect(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    connection = _quickbooks_connection(db, tenant_id=tenant_id)
    if connection is None:
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': 'QuickBooks connection was not found.'})}",
            status_code=303,
        )

    previous_realm_id = str(connection.realm_id or "").strip() or None
    disconnect_quickbooks_connection(connection)
    audit_log(
        db,
        request,
        action="ACCOUNTING_DISCONNECTED",
        entity_type="accounting_connection",
        entity_id=connection.id,
        summary="Disconnected QuickBooks company",
        details={
            "provider": QUICKBOOKS_PROVIDER,
            "realm_id": previous_realm_id,
        },
    )
    _record_accounting_event(
        db,
        tenant_id=tenant_id,
        event_type="oauth_disconnected",
        direction="OUTBOUND",
        summary="Disconnected QuickBooks company",
        entity_id=connection.id,
        detail_json={"realm_id": previous_realm_id},
    )
    db.commit()
    return RedirectResponse(
        url="/admin/accounting?quickbooks_disconnected=1",
        status_code=303,
    )


@router.post("/admin/accounting/run-sync")
async def admin_accounting_run_sync(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    form = await request.form()
    retry_failed = str(form.get("retry_failed", "") or "").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    result = process_pending_accounting_jobs(
        db,
        tenant_id=int(tenant_id),
        provider=QUICKBOOKS_PROVIDER,
        limit=5,
        retry_failed=retry_failed,
    )
    audit_log(
        db,
        request,
        action="ACCOUNTING_SYNC_RUN",
        entity_type="accounting_connection",
        summary="Ran QuickBooks accounting sync jobs",
        details={
            "provider": QUICKBOOKS_PROVIDER,
            "retry_failed": retry_failed,
            "processed": result.processed,
            "succeeded": result.succeeded,
            "failed": result.failed,
        },
    )
    db.commit()
    query = {
        "sync_run": "1",
        "sync_processed": str(result.processed),
        "sync_succeeded": str(result.succeeded),
        "sync_failed": str(result.failed),
    }
    if retry_failed:
        query["sync_retry_run"] = "1"
    return RedirectResponse(
        url=f"/admin/accounting?{urlencode(query)}",
        status_code=303,
    )
