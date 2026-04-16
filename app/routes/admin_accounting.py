from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    User,
)
from ..models.base import utcnow
from ..permissions import PERM_MANAGE_SETTINGS, require_permission
from ..services.accounting.job_runner import process_pending_accounting_jobs
from ..services.accounting.quickbooks_oauth import (
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
from ..tenancy import request_platform_mode, request_tenant_id
from ..user_roles import ROLE_TENANT_ADMIN

router = APIRouter()
_OAUTH_STATE_SESSION_KEY = "accounting_quickbooks_oauth_state"
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


def _configure_state_bucket(request: Request) -> dict[str, dict[str, object]]:
    raw_value = request.session.get(_OAUTH_STATE_SESSION_KEY)
    states = raw_value if isinstance(raw_value, dict) else {}
    cutoff = utcnow() - _OAUTH_STATE_TTL
    pruned: dict[str, dict[str, object]] = {}
    for key, payload in states.items():
        if not isinstance(payload, dict):
            continue
        issued_at = payload.get("issued_at")
        if not isinstance(issued_at, str):
            continue
        try:
            issued_at_value = datetime.fromisoformat(issued_at)
        except ValueError:
            continue
        if issued_at_value < cutoff:
            continue
        pruned[str(key)] = payload
    request.session[_OAUTH_STATE_SESSION_KEY] = pruned
    return pruned


def _issue_quickbooks_state(
    request: Request,
    *,
    tenant_id: int,
    user_id: int,
) -> str:
    states = _configure_state_bucket(request)
    state = secrets.token_urlsafe(32)
    states[state] = {
        "tenant_id": int(tenant_id),
        "user_id": int(user_id),
        "issued_at": utcnow().isoformat(),
    }
    request.session[_OAUTH_STATE_SESSION_KEY] = states
    return state


def _consume_quickbooks_state(
    request: Request,
    *,
    state: str,
    tenant_id: int,
    user_id: int,
) -> bool:
    states = _configure_state_bucket(request)
    payload = states.pop(str(state or "").strip(), None)
    request.session[_OAUTH_STATE_SESSION_KEY] = states
    if not isinstance(payload, dict):
        return False
    if int(payload.get("tenant_id") or 0) != int(tenant_id):
        return False
    if int(payload.get("user_id") or 0) != int(user_id):
        return False
    issued_at_raw = payload.get("issued_at")
    if not isinstance(issued_at_raw, str):
        return False
    try:
        issued_at = datetime.fromisoformat(issued_at_raw)
    except ValueError:
        return False
    return issued_at >= utcnow() - _OAUTH_STATE_TTL


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


@router.get("/admin/accounting/quickbooks/callback")
def admin_accounting_quickbooks_callback(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    current_user = _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    user_id = int(current_user.id)
    state = str(request.query_params.get("state", "") or "").strip()
    code = str(request.query_params.get("code", "") or "").strip()
    realm_id = str(request.query_params.get("realmId", "") or "").strip() or None
    provider_error = str(request.query_params.get("error", "") or "").strip()
    provider_error_description = str(
        request.query_params.get("error_description", "") or ""
    ).strip()

    if not state or not _consume_quickbooks_state(
        request,
        state=state,
        tenant_id=tenant_id,
        user_id=user_id,
    ):
        error_text = "QuickBooks callback could not be verified."
        audit_log(
            db,
            request,
            action="ACCOUNTING_CALLBACK_FAILED",
            entity_type="accounting_connection",
            summary="QuickBooks callback failed state validation",
            details={"provider": QUICKBOOKS_PROVIDER, "error": error_text},
        )
        _record_accounting_event(
            db,
            tenant_id=tenant_id,
            event_type="oauth_callback_failed",
            direction="INBOUND",
            summary="QuickBooks callback failed state validation",
            detail_json={"error": error_text},
        )
        db.commit()
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': error_text})}",
            status_code=303,
        )

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
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': error_text or 'QuickBooks callback failed.'})}",
            status_code=303,
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
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': error_text})}",
            status_code=303,
        )

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
        return RedirectResponse(
            url=f"/admin/accounting?{urlencode({'error': error_text})}",
            status_code=303,
        )

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
    return RedirectResponse(url="/admin/accounting?quickbooks_connected=1", status_code=303)


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
