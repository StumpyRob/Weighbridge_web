from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import AccountingConnection
from ...models.base import utcnow
from ..secrets import decrypt_string, encrypt_string

QUICKBOOKS_PROVIDER = "quickbooks"
QUICKBOOKS_SCOPE = "com.intuit.quickbooks.accounting"
_QUICKBOOKS_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
_QUICKBOOKS_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_QUICKBOOKS_API_BASE_BY_ENV = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class QuickBooksOAuthError(ValueError):
    pass


@dataclass(frozen=True)
class QuickBooksTokenBundle:
    access_token: str
    refresh_token: str
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    scopes: str | None
    realm_id: str | None
    raw_response: dict[str, object]


def _normalized_environment() -> str:
    raw = str(settings.quickbooks_environment or "").strip().lower()
    if not raw:
        return "sandbox"
    if raw in _QUICKBOOKS_API_BASE_BY_ENV:
        return raw
    raise QuickBooksOAuthError(
        "QUICKBOOKS_ENVIRONMENT must be 'sandbox' or 'production'."
    )


def _validated_client_id() -> str:
    client_id = str(settings.quickbooks_client_id or "").strip()
    if not client_id:
        raise QuickBooksOAuthError("QUICKBOOKS_CLIENT_ID is not configured.")
    return client_id


def _validated_client_secret() -> str:
    client_secret = str(settings.quickbooks_client_secret or "").strip()
    if not client_secret:
        raise QuickBooksOAuthError("QUICKBOOKS_CLIENT_SECRET is not configured.")
    return client_secret


def resolve_quickbooks_redirect_uri(request: Request | None = None) -> str:
    configured = str(settings.quickbooks_redirect_uri or "").strip()
    if configured and request is not None:
        tenant_subdomain = str(
            getattr(getattr(request, "state", None), "request_subdomain", "") or ""
        ).strip()
        tenant_route_prefix = str(
            getattr(getattr(request, "state", None), "tenant_route_prefix", "") or ""
        ).strip()
        configured = configured.replace("{tenant_subdomain}", tenant_subdomain)
        configured = configured.replace("{tenant_route_prefix}", tenant_route_prefix)
    if configured:
        return configured
    if request is None:
        raise QuickBooksOAuthError("QUICKBOOKS_REDIRECT_URI is not configured.")
    origin = str(request.base_url).rstrip("/")
    tenant_route_prefix = str(
        getattr(getattr(request, "state", None), "tenant_route_prefix", "") or ""
    ).strip()
    callback_path = f"{tenant_route_prefix}/admin/accounting/quickbooks/callback"
    return f"{origin}{callback_path}"


def quickbooks_api_base_url() -> str:
    return _QUICKBOOKS_API_BASE_BY_ENV[_normalized_environment()]


def build_quickbooks_authorize_url(
    *,
    state: str,
    redirect_uri: str,
) -> str:
    client_id = _validated_client_id()
    _normalized_environment()
    resolved_redirect_uri = str(redirect_uri or "").strip()
    if not resolved_redirect_uri:
        raise QuickBooksOAuthError("QUICKBOOKS_REDIRECT_URI is not configured.")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": resolved_redirect_uri,
            "response_type": "code",
            "scope": QUICKBOOKS_SCOPE,
            "state": str(state or "").strip(),
        }
    )
    return f"{_QUICKBOOKS_AUTHORIZE_URL}?{query}"


def _token_expiry(
    raw_seconds: object,
    *,
    default_seconds: int | None = None,
) -> datetime | None:
    if raw_seconds in (None, ""):
        raw_seconds = default_seconds
    if raw_seconds in (None, ""):
        return None
    try:
        seconds = int(raw_seconds)
    except (TypeError, ValueError):
        return None
    return utcnow() + timedelta(seconds=max(0, seconds))


def _token_bundle_from_payload(
    payload: dict[str, object],
    *,
    realm_id: str | None,
) -> QuickBooksTokenBundle:
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise QuickBooksOAuthError("QuickBooks token response was incomplete.")
    scope_value = payload.get("scope")
    scopes = (
        " ".join(str(item).strip() for item in scope_value if str(item).strip())
        if isinstance(scope_value, list)
        else str(scope_value or "").strip()
    )
    return QuickBooksTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=_token_expiry(payload.get("expires_in")),
        refresh_token_expires_at=_token_expiry(
            payload.get("x_refresh_token_expires_in")
        ),
        scopes=scopes or None,
        realm_id=str(realm_id or "").strip() or None,
        raw_response=dict(payload),
    )


def exchange_code_for_tokens(
    *,
    code: str,
    redirect_uri: str,
    realm_id: str | None,
) -> QuickBooksTokenBundle:
    resolved_code = str(code or "").strip()
    resolved_redirect_uri = str(redirect_uri or "").strip()
    if not resolved_code:
        raise QuickBooksOAuthError("QuickBooks authorization code is missing.")
    if not resolved_redirect_uri:
        raise QuickBooksOAuthError("QUICKBOOKS_REDIRECT_URI is not configured.")

    response = None
    try:
        response = httpx.post(
            _QUICKBOOKS_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": resolved_code,
                "redirect_uri": resolved_redirect_uri,
            },
            auth=(_validated_client_id(), _validated_client_secret()),
            headers={"Accept": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = str(getattr(getattr(exc, "response", None), "text", "") or "").strip()
        raise QuickBooksOAuthError(
            f"QuickBooks token exchange failed: {detail or exc.response.reason_phrase or 'request rejected.'}"
        ) from exc
    except httpx.RequestError as exc:
        raise QuickBooksOAuthError(
            "QuickBooks token exchange failed: network request error."
        ) from exc
    except ValueError as exc:
        raise QuickBooksOAuthError(
            "QuickBooks token exchange failed: invalid JSON response."
        ) from exc

    if not isinstance(payload, dict):
        raise QuickBooksOAuthError("QuickBooks token response was invalid.")
    return _token_bundle_from_payload(payload, realm_id=realm_id)


def refresh_tokens(
    *,
    refresh_token: str,
    realm_id: str | None = None,
) -> QuickBooksTokenBundle:
    decrypted_refresh_token = decrypt_string(refresh_token)
    resolved_refresh_token = str(decrypted_refresh_token or "").strip()
    if not resolved_refresh_token:
        raise QuickBooksOAuthError("QuickBooks refresh token is missing.")

    try:
        response = httpx.post(
            _QUICKBOOKS_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": resolved_refresh_token,
            },
            auth=(_validated_client_id(), _validated_client_secret()),
            headers={"Accept": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = str(getattr(getattr(exc, "response", None), "text", "") or "").strip()
        raise QuickBooksOAuthError(
            f"QuickBooks token refresh failed: {detail or exc.response.reason_phrase or 'request rejected.'}"
        ) from exc
    except httpx.RequestError as exc:
        raise QuickBooksOAuthError(
            "QuickBooks token refresh failed: network request error."
        ) from exc
    except ValueError as exc:
        raise QuickBooksOAuthError(
            "QuickBooks token refresh failed: invalid JSON response."
        ) from exc

    if not isinstance(payload, dict):
        raise QuickBooksOAuthError("QuickBooks token refresh response was invalid.")
    return _token_bundle_from_payload(payload, realm_id=realm_id)


def get_or_create_quickbooks_connection(
    db: Session,
    *,
    tenant_id: int,
) -> AccountingConnection:
    connection = (
        db.execute(
            select(AccountingConnection).where(
                AccountingConnection.tenant_id == int(tenant_id),
                AccountingConnection.provider == QUICKBOOKS_PROVIDER,
            )
        )
        .scalars()
        .first()
    )
    if connection is not None:
        return connection

    connection = AccountingConnection(
        tenant_id=int(tenant_id),
        provider=QUICKBOOKS_PROVIDER,
        status="disconnected",
    )
    db.add(connection)
    db.flush()
    return connection


def store_quickbooks_tokens(
    connection: AccountingConnection,
    *,
    token_bundle: QuickBooksTokenBundle,
) -> AccountingConnection:
    connection.provider = QUICKBOOKS_PROVIDER
    connection.status = "connected"
    connection.realm_id = token_bundle.realm_id
    connection.encrypted_access_token = encrypt_string(token_bundle.access_token)
    connection.encrypted_refresh_token = encrypt_string(token_bundle.refresh_token)
    connection.access_token_expires_at = token_bundle.access_token_expires_at
    connection.refresh_token_expires_at = token_bundle.refresh_token_expires_at
    connection.scopes = token_bundle.scopes
    connection.connected_at = utcnow()
    connection.disconnected_at = None
    connection.last_error = None
    connection.updated_at = utcnow()
    return connection


def disconnect_quickbooks_connection(
    connection: AccountingConnection,
) -> AccountingConnection:
    connection.provider = QUICKBOOKS_PROVIDER
    connection.status = "disconnected"
    connection.encrypted_access_token = None
    connection.encrypted_refresh_token = None
    connection.access_token_expires_at = None
    connection.refresh_token_expires_at = None
    connection.scopes = None
    connection.disconnected_at = utcnow()
    connection.last_error = None
    connection.updated_at = utcnow()
    return connection
