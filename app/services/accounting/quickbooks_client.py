from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ...models import AccountingConnection
from ...models.base import utcnow
from ..secrets import decrypt_string, encrypt_string
from .quickbooks_oauth import (
    QUICKBOOKS_PROVIDER,
    QuickBooksOAuthError,
    quickbooks_api_base_url,
    refresh_tokens,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_TOKEN_REFRESH_SKEW = timedelta(seconds=60)
_ENTITY_RESPONSE_KEYS = {
    "customer": "Customer",
    "item": "Item",
    "invoice": "Invoice",
    "payment": "Payment",
    "taxcode": "TaxCode",
}


class QuickBooksApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail_json: dict[str, Any] | None = None,
        auth_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail_json = detail_json or {}
        self.auth_error = auth_error


class QuickBooksUnsupportedError(QuickBooksApiError):
    pass


@dataclass(frozen=True)
class QuickBooksEntityResult:
    status_code: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class QuickBooksRevenueAccount:
    remote_account_id: str
    remote_account_code: str | None
    remote_account_name: str
    remote_account_type: str | None
    remote_account_detail_type: str | None
    is_active: bool
    is_usable: bool


@dataclass(frozen=True)
class QuickBooksTaxCode:
    remote_tax_code_id: str
    display_code: str
    display_name: str | None
    description: str | None
    is_active: bool


def quote_query_value(value: object) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _entity_response_key(entity_name: str) -> str:
    normalized = str(entity_name or "").strip().lower()
    return _ENTITY_RESPONSE_KEYS.get(normalized, normalized.title())


def _entity_path_segment(entity_name: str) -> str:
    return str(entity_name or "").strip().lower()


def _safe_number(value: object) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric.is_integer():
        return int(numeric)
    return numeric


def _normalized_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _account_is_revenue(account: dict[str, Any]) -> bool:
    account_type = str(account.get("AccountType") or "").strip().lower()
    classification = str(account.get("Classification") or "").strip().lower()
    return account_type == "income" or classification == "revenue"


def _account_name(account: dict[str, Any]) -> str:
    return (
        str(account.get("FullyQualifiedName") or "").strip()
        or str(account.get("Name") or "").strip()
        or f"Account {str(account.get('Id') or '').strip() or '?'}"
    )


def _quickbooks_revenue_account(account: dict[str, Any]) -> QuickBooksRevenueAccount | None:
    resolved_id = str(account.get("Id") or "").strip()
    if not resolved_id:
        return None
    return QuickBooksRevenueAccount(
        remote_account_id=resolved_id,
        remote_account_code=_normalized_text(account.get("AcctNum")),
        remote_account_name=_account_name(account),
        remote_account_type=_normalized_text(account.get("AccountType"))
        or _normalized_text(account.get("Classification")),
        remote_account_detail_type=_normalized_text(account.get("AccountSubType")),
        is_active=bool(account.get("Active", True)),
        is_usable=_account_is_revenue(account),
    )


def _quickbooks_tax_code(tax_code: dict[str, Any]) -> QuickBooksTaxCode | None:
    resolved_id = str(tax_code.get("Id") or "").strip()
    display_code = (
        str(tax_code.get("Name") or "").strip()
        or str(tax_code.get("Code") or "").strip()
    )
    if not resolved_id or not display_code:
        return None
    description = _normalized_text(tax_code.get("Description"))
    display_name = description or display_code
    return QuickBooksTaxCode(
        remote_tax_code_id=resolved_id,
        display_code=display_code,
        display_name=display_name,
        description=description,
        is_active=bool(tax_code.get("Active", True)),
    )


def _fault_detail(payload: object, *, status_code: int, fallback_text: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "status_code": status_code,
            "message": fallback_text,
        }
    fault = payload.get("Fault")
    if not isinstance(fault, dict):
        return {
            "status_code": status_code,
            "message": fallback_text,
        }
    errors = fault.get("Error")
    error = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(error, dict):
        error = {}
    return {
        "status_code": status_code,
        "fault_type": str(fault.get("type") or "").strip() or None,
        "code": str(error.get("code") or "").strip() or None,
        "message": str(error.get("Message") or "").strip() or None,
        "detail": str(error.get("Detail") or "").strip() or None,
    }


def _fault_message(detail_json: dict[str, Any], *, fallback_text: str) -> str:
    detail = str(detail_json.get("detail") or "").strip()
    message = str(detail_json.get("message") or "").strip()
    if detail:
        return detail
    if message:
        return message
    return fallback_text


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise QuickBooksApiError(
            "QuickBooks returned invalid JSON.",
            status_code=response.status_code,
            detail_json={"status_code": response.status_code},
        ) from exc
    if isinstance(payload, dict):
        return payload
    raise QuickBooksApiError(
        "QuickBooks returned an unexpected response payload.",
        status_code=response.status_code,
        detail_json={"status_code": response.status_code},
    )


def compact_quickbooks_entity(entity: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    summary = {
        "id": str(entity.get("Id") or "").strip() or None,
        "sync_token": str(entity.get("SyncToken") or "").strip() or None,
        "doc_number": str(entity.get("DocNumber") or "").strip() or None,
        "display_name": str(entity.get("DisplayName") or "").strip() or None,
        "name": str(entity.get("Name") or "").strip() or None,
        "txn_date": str(entity.get("TxnDate") or "").strip() or None,
    }
    balance = _safe_number(entity.get("Balance"))
    if balance is not None:
        summary["balance"] = balance
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


class QuickBooksClient:
    def __init__(self, db: Session, connection: AccountingConnection) -> None:
        self.db = db
        self.connection = connection
        self._active_accounts_cache: list[dict[str, Any]] | None = None
        self._tax_codes_cache: list[dict[str, Any]] | None = None

    @property
    def realm_id(self) -> str:
        realm_id = str(self.connection.realm_id or "").strip()
        if not realm_id:
            self._mark_connection_error("QuickBooks realm ID is missing.")
            raise QuickBooksApiError(
                "QuickBooks realm ID is missing.",
                auth_error=True,
            )
        return realm_id

    def _mark_connection_error(self, message: str) -> None:
        self.connection.status = "error"
        self.connection.last_error = str(message or "").strip() or None
        self.connection.updated_at = utcnow()

    def _access_token_expired(self) -> bool:
        if not str(self.connection.encrypted_access_token or "").strip():
            return True
        expires_at = self.connection.access_token_expires_at
        if expires_at is None:
            return True
        return expires_at <= (utcnow() + _TOKEN_REFRESH_SKEW)

    def _access_token(self) -> str:
        encrypted_access_token = str(self.connection.encrypted_access_token or "").strip()
        if not encrypted_access_token:
            self._refresh_access_token()
            encrypted_access_token = str(self.connection.encrypted_access_token or "").strip()
        elif self._access_token_expired():
            self._refresh_access_token()
            encrypted_access_token = str(self.connection.encrypted_access_token or "").strip()

        try:
            access_token = decrypt_string(encrypted_access_token)
        except Exception as exc:  # pragma: no cover - defensive branch
            self._mark_connection_error("QuickBooks access token could not be decrypted.")
            raise QuickBooksApiError(
                "QuickBooks access token could not be decrypted.",
                auth_error=True,
            ) from exc
        resolved_access_token = str(access_token or "").strip()
        if not resolved_access_token:
            self._mark_connection_error("QuickBooks access token is missing.")
            raise QuickBooksApiError(
                "QuickBooks access token is missing.",
                auth_error=True,
            )
        return resolved_access_token

    def _refresh_access_token(self) -> None:
        encrypted_refresh_token = str(self.connection.encrypted_refresh_token or "").strip()
        if not encrypted_refresh_token:
            self._mark_connection_error("QuickBooks refresh token is missing.")
            raise QuickBooksApiError(
                "QuickBooks refresh token is missing.",
                auth_error=True,
            )
        try:
            token_bundle = refresh_tokens(
                refresh_token=encrypted_refresh_token,
                realm_id=self.connection.realm_id,
            )
        except QuickBooksOAuthError as exc:
            message = str(exc)
            self._mark_connection_error(message)
            raise QuickBooksApiError(
                message,
                auth_error=True,
            ) from exc

        self.connection.status = "connected"
        self.connection.encrypted_access_token = encrypt_string(token_bundle.access_token)
        self.connection.encrypted_refresh_token = encrypt_string(token_bundle.refresh_token)
        self.connection.access_token_expires_at = token_bundle.access_token_expires_at
        self.connection.refresh_token_expires_at = token_bundle.refresh_token_expires_at
        self.connection.scopes = token_bundle.scopes
        if token_bundle.realm_id:
            self.connection.realm_id = token_bundle.realm_id
        self.connection.disconnected_at = None
        self.connection.last_error = None
        self.connection.updated_at = utcnow()
        self.db.flush()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_retry: bool = True,
    ) -> QuickBooksEntityResult:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token()}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        url = f"{quickbooks_api_base_url()}{path}"
        try:
            response = httpx.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise QuickBooksApiError(
                "QuickBooks request failed: network request error.",
                detail_json={
                    "method": method.upper(),
                    "path": path,
                },
            ) from exc

        if response.status_code == 401 and allow_retry:
            logger.info("QuickBooks request received 401, attempting token refresh.")
            self._refresh_access_token()
            return self._request(
                method,
                path,
                params=params,
                json_body=json_body,
                allow_retry=False,
            )

        payload = _response_payload(response) if response.content else {}
        if 200 <= response.status_code < 300:
            self.connection.status = "connected"
            self.connection.last_error = None
            self.connection.updated_at = utcnow()
            return QuickBooksEntityResult(status_code=response.status_code, payload=payload)

        detail_json = _fault_detail(
            payload,
            status_code=response.status_code,
            fallback_text=response.reason_phrase or "QuickBooks request failed.",
        )
        message = _fault_message(
            detail_json,
            fallback_text="QuickBooks request failed.",
        )
        auth_error = response.status_code in {401, 403}
        if auth_error:
            self._mark_connection_error(message)
        raise QuickBooksApiError(
            message,
            status_code=response.status_code,
            detail_json=detail_json,
            auth_error=auth_error,
        )

    def query(self, sql: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/v3/company/{self.realm_id}/query",
            params={"query": str(sql or "").strip()},
        )
        query_response = result.payload.get("QueryResponse")
        if isinstance(query_response, dict):
            return query_response
        return {}

    def query_entities(self, entity_name: str, sql: str) -> list[dict[str, Any]]:
        query_response = self.query(sql)
        value = query_response.get(_entity_response_key(entity_name))
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def get_entity(
        self,
        entity_name: str,
        external_id: str,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        try:
            result = self._request(
                "GET",
                f"/v3/company/{self.realm_id}/{_entity_path_segment(entity_name)}/{external_id}",
            )
        except QuickBooksApiError as exc:
            if allow_not_found and exc.status_code in {400, 404}:
                message = str(exc).lower()
                if "not found" in message or "object not found" in message:
                    return None
            raise
        entity = result.payload.get(_entity_response_key(entity_name))
        if isinstance(entity, dict):
            return entity
        if allow_not_found:
            return None
        raise QuickBooksApiError(f"QuickBooks {entity_name} response was incomplete.")

    def create_entity(self, entity_name: str, payload: dict[str, Any]) -> QuickBooksEntityResult:
        result = self._request(
            "POST",
            f"/v3/company/{self.realm_id}/{_entity_path_segment(entity_name)}",
            json_body=payload,
        )
        entity = result.payload.get(_entity_response_key(entity_name))
        if not isinstance(entity, dict):
            raise QuickBooksApiError(f"QuickBooks {entity_name} create response was incomplete.")
        return QuickBooksEntityResult(status_code=result.status_code, payload=entity)

    def update_entity(self, entity_name: str, payload: dict[str, Any]) -> QuickBooksEntityResult:
        result = self._request(
            "POST",
            f"/v3/company/{self.realm_id}/{_entity_path_segment(entity_name)}",
            params={"operation": "update"},
            json_body=payload,
        )
        entity = result.payload.get(_entity_response_key(entity_name))
        if not isinstance(entity, dict):
            raise QuickBooksApiError(f"QuickBooks {entity_name} update response was incomplete.")
        return QuickBooksEntityResult(status_code=result.status_code, payload=entity)

    def create_payment(self, payload: dict[str, Any]) -> QuickBooksEntityResult:
        return self.create_entity("payment", payload)

    def _active_accounts(self) -> list[dict[str, Any]]:
        if self._active_accounts_cache is None:
            self._active_accounts_cache = self.query_entities(
                "account",
                "SELECT * FROM Account WHERE Active = true",
            )
        return list(self._active_accounts_cache)

    def _tax_codes(self) -> list[dict[str, Any]]:
        if self._tax_codes_cache is None:
            self._tax_codes_cache = self.query_entities(
                "taxcode",
                "SELECT * FROM TaxCode",
            )
        return list(self._tax_codes_cache)

    def list_revenue_accounts(self) -> list[QuickBooksRevenueAccount]:
        revenue_accounts: list[QuickBooksRevenueAccount] = []
        for account in self._active_accounts():
            if not _account_is_revenue(account):
                continue
            resolved = _quickbooks_revenue_account(account)
            if resolved is None:
                continue
            revenue_accounts.append(resolved)
        revenue_accounts.sort(
            key=lambda account: (
                account.remote_account_code is None,
                str(account.remote_account_code or ""),
                str(account.remote_account_name or "").lower(),
                str(account.remote_account_id or ""),
            )
        )
        return revenue_accounts

    def list_tax_codes(self) -> list[QuickBooksTaxCode]:
        tax_codes: list[QuickBooksTaxCode] = []
        for tax_code in self._tax_codes():
            resolved = _quickbooks_tax_code(tax_code)
            if resolved is None:
                continue
            tax_codes.append(resolved)
        tax_codes.sort(
            key=lambda tax_code: (
                not tax_code.is_active,
                str(tax_code.display_code or "").lower(),
                str(tax_code.display_name or "").lower(),
                str(tax_code.remote_tax_code_id or ""),
            )
        )
        return tax_codes

    def resolve_income_account_by_id(self, *, remote_account_id: str) -> QuickBooksRevenueAccount:
        normalized_account_id = str(remote_account_id or "").strip()
        if not normalized_account_id:
            raise QuickBooksApiError("Mapped QuickBooks revenue account ID is missing.")
        for account in self.list_revenue_accounts():
            if str(account.remote_account_id) == normalized_account_id:
                return account
        raise QuickBooksApiError(
            f"QuickBooks revenue account {normalized_account_id} was not found among active income/revenue accounts."
        )

    def resolve_income_account_by_nominal_code(self, *, nominal_code: str) -> QuickBooksRevenueAccount:
        normalized_nominal_code = str(nominal_code or "").strip()
        if not normalized_nominal_code:
            raise QuickBooksApiError(
                "Local nominal code is missing; QuickBooks income account cannot be resolved safely."
            )
        candidates = self._active_accounts()
        exact_matches = [
            account
            for account in candidates
            if str(account.get("AcctNum") or "").strip() == normalized_nominal_code
        ]
        if not exact_matches:
            raise QuickBooksApiError(
                f"QuickBooks income account with AcctNum {normalized_nominal_code} was not found."
            )

        revenue_matches: list[dict[str, Any]] = []
        for account in exact_matches:
            if _account_is_revenue(account):
                revenue_matches.append(account)

        if not revenue_matches:
            raise QuickBooksApiError(
                f"QuickBooks account {normalized_nominal_code} exists but is not an income/revenue account."
            )
        if len(revenue_matches) > 1:
            raise QuickBooksApiError(
                f"Multiple QuickBooks income accounts matched AcctNum {normalized_nominal_code}."
            )

        resolved_id = str(revenue_matches[0].get("Id") or "").strip()
        if not resolved_id:
            raise QuickBooksApiError(
                f"QuickBooks income account {normalized_nominal_code} did not include an Id."
            )
        resolved_account = _quickbooks_revenue_account(revenue_matches[0])
        if resolved_account is None:
            raise QuickBooksApiError(
                f"QuickBooks income account {normalized_nominal_code} did not include an Id."
            )
        return resolved_account

    def resolve_income_account_ref(self, *, nominal_code: str) -> str:
        return self.resolve_income_account_ref_by_nominal_code(
            nominal_code=nominal_code
        )

    def resolve_income_account_ref_by_nominal_code(self, *, nominal_code: str) -> str:
        return self.resolve_income_account_by_nominal_code(
            nominal_code=nominal_code
        ).remote_account_id

    def resolve_income_account_ref_by_id(self, *, remote_account_id: str) -> str:
        return self.resolve_income_account_by_id(
            remote_account_id=remote_account_id
        ).remote_account_id

    def void_invoice(self, *, external_id: str, sync_token: str) -> QuickBooksEntityResult:
        raise QuickBooksUnsupportedError(
            "QuickBooks invoice void sync is not supported yet.",
            detail_json={
                "external_id": str(external_id or "").strip() or None,
                "sync_token": str(sync_token or "").strip() or None,
            },
        )


def quickbooks_client_for_connection(
    db: Session,
    connection: AccountingConnection,
) -> QuickBooksClient:
    if str(connection.provider or "").strip().lower() != QUICKBOOKS_PROVIDER:
        raise QuickBooksApiError("Unsupported accounting provider.")
    return QuickBooksClient(db, connection)
