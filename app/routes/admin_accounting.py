from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
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
    InvoiceLine,
    AccountingSyncEvent,
    AccountingSyncJob,
    Product,
    Tenant,
    Ticket,
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
from ..services.accounting.quickbooks_client import QuickBooksApiError
from ..services.accounting.revenue_account_mapping import (
    RevenueAccountMappingValidationError,
    clear_default_revenue_account_mapping,
    get_default_revenue_account_mapping,
    list_provider_revenue_accounts,
    save_default_revenue_account_mapping,
)
from ..services.accounting.tax_mapping import (
    TaxMappingValidationError,
    create_quickbooks_tax_mapping,
    delete_quickbooks_tax_mapping,
    inspect_quickbooks_tax_discovery,
    list_provider_tax_codes,
    summarize_quickbooks_setup,
    update_quickbooks_tax_mapping,
)
from ..templating import templates
from ..tenancy import request_platform_mode, request_tenant_id, tenant_request_url
from ..user_roles import ROLE_TENANT_ADMIN

router = APIRouter()
_OAUTH_STATE_TTL = timedelta(minutes=10)
_ACCTNUM_ERROR_PATTERNS = (
    re.compile(r"\bAcctNum\s+(?P<acct>[A-Za-z0-9._/-]+)\b", re.IGNORECASE),
    re.compile(
        r"\baccount\s+(?P<acct>[A-Za-z0-9._/-]+)\s+exists but is not\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bincome account\s+(?P<acct>[A-Za-z0-9._/-]+)\s+did not include an Id\b",
        re.IGNORECASE,
    ),
)


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _revenue_account_display_label(account: object) -> str:
    code = _normalize_text(getattr(account, "remote_account_code", None))
    name = (
        _normalize_text(getattr(account, "remote_account_name", None))
        or f"Account {str(getattr(account, 'remote_account_id', '') or '').strip() or '?'}"
    )
    account_type = _normalize_text(getattr(account, "remote_account_type", None)) or "Income"
    base_label = f"{code} - {name}" if code else name
    return f"{base_label} ({account_type})"


def _is_income_revenue_account_option(account: object) -> bool:
    if not bool(getattr(account, "is_usable", False)):
        return False
    account_type = str(getattr(account, "remote_account_type", "") or "").strip().lower()
    return account_type == "income" or not account_type


def _revenue_account_option_rows(accounts: list[object]) -> list[dict[str, object]]:
    option_rows = [
        {
            "remote_account_id": str(getattr(account, "remote_account_id", "") or "").strip(),
            "remote_account_code": _normalize_text(getattr(account, "remote_account_code", None)),
            "remote_account_name": _normalize_text(getattr(account, "remote_account_name", None)),
            "remote_account_type": _normalize_text(getattr(account, "remote_account_type", None)),
            "display_label": _revenue_account_display_label(account),
        }
        for account in accounts
        if _is_income_revenue_account_option(account)
        and str(getattr(account, "remote_account_id", "") or "").strip()
    ]
    option_rows.sort(
        key=lambda account: (
            str("sales" not in str(account["remote_account_name"] or "").lower()),
            str(account["remote_account_code"] or "").lower(),
            str(account["remote_account_name"] or "").lower(),
            str(account["remote_account_id"] or ""),
        )
    )
    return option_rows


def _suggested_revenue_account_id(account_rows: list[dict[str, object]]) -> str | None:
    if not account_rows:
        return None
    sales_match = next(
        (
            row
            for row in account_rows
            if "sales" in str(row.get("remote_account_name") or "").lower()
        ),
        None,
    )
    selected = sales_match or account_rows[0]
    return str(selected.get("remote_account_id") or "").strip() or None


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


def _snapshot_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _snapshot_int(snapshot: dict[str, object], key: str) -> int | None:
    raw = snapshot.get(key)
    try:
        resolved = int(raw)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _snapshot_text(snapshot: dict[str, object], key: str) -> str | None:
    value = snapshot.get(key)
    text = str(value or "").strip()
    return text or None


def _requested_acctnum(error_text: str | None) -> str | None:
    message = str(error_text or "").strip()
    if not message:
        return None
    for pattern in _ACCTNUM_ERROR_PATTERNS:
        match = pattern.search(message)
        if match:
            acctnum = str(match.group("acct") or "").strip()
            if acctnum:
                return acctnum
    return None


def _invoice_line_product(
    db: Session,
    *,
    tenant_id: int,
    snapshot: dict[str, object],
    ticket: Ticket | None,
    product: Product | None,
) -> Product | None:
    snapshot_product_id = _snapshot_int(snapshot, "product_id")
    if product is not None:
        if snapshot_product_id is None or int(product.id or 0) == snapshot_product_id:
            return product
    if snapshot_product_id is not None:
        return (
            db.execute(
                select(Product).where(
                    Product.id == int(snapshot_product_id),
                    Product.tenant_id == int(tenant_id),
                )
            )
            .scalars()
            .first()
        )
    if product is not None:
        return product
    if ticket is not None and ticket.product_id is not None:
        return (
            db.execute(
                select(Product).where(
                    Product.id == int(ticket.product_id),
                    Product.tenant_id == int(tenant_id),
                )
            )
            .scalars()
            .first()
        )
    return None


def _invoice_line_nominal_source(
    *,
    product: Product | None,
    snapshot: dict[str, object],
    requested_acctnum: str,
) -> tuple[str, str | None]:
    direct_nominal = str(getattr(product, "nominal_code", "") or "").strip() or None
    group_default = (
        str(getattr(getattr(product, "product_group", None), "nominal_code_default", "") or "").strip()
        or None
    )
    snapshot_nominal = _snapshot_text(snapshot, "nominal_code")
    candidates: list[tuple[str, str | None]] = []
    if direct_nominal:
        candidates.append(("product nominal code", direct_nominal))
    if group_default:
        candidates.append(("product group default nominal code", group_default))
    if snapshot_nominal:
        candidates.append(("invoice snapshot nominal code", snapshot_nominal))
    for source_label, nominal_code in candidates:
        if nominal_code == requested_acctnum:
            return source_label, nominal_code
    if candidates:
        return candidates[0]
    return "nominal code source unavailable", None


def _invoice_account_mismatch_context(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    error_text: str | None,
) -> dict[str, object] | None:
    requested_acctnum = _requested_acctnum(error_text)
    if requested_acctnum is None:
        return None

    line_rows = list(
        db.execute(
            select(InvoiceLine, Ticket, Product)
            .outerjoin(
                Ticket,
                (Ticket.id == InvoiceLine.ticket_id)
                & (Ticket.tenant_id == InvoiceLine.tenant_id),
            )
            .outerjoin(
                Product,
                (Product.id == Ticket.product_id)
                & (Product.tenant_id == Ticket.tenant_id),
            )
            .where(
                InvoiceLine.tenant_id == int(tenant_id),
                InvoiceLine.invoice_id == int(invoice_id),
            )
            .order_by(InvoiceLine.id.asc())
        ).all()
    )

    product_sources: list[dict[str, object]] = []
    seen_sources: set[tuple[object, ...]] = set()
    for line, ticket, product in line_rows:
        snapshot = _snapshot_dict(getattr(line, "product_snapshot_json", None))
        resolved_product = _invoice_line_product(
            db,
            tenant_id=int(tenant_id),
            snapshot=snapshot,
            ticket=ticket,
            product=product,
        )
        product_id = (
            int(getattr(resolved_product, "id", 0) or 0)
            or _snapshot_int(snapshot, "product_id")
            or int(getattr(ticket, "product_id", 0) or 0)
            or None
        )
        product_code = (
            str(getattr(resolved_product, "code", "") or "").strip()
            or _snapshot_text(snapshot, "product_code")
        )
        product_description = (
            str(getattr(resolved_product, "description", "") or "").strip() or None
        )
        nominal_source, nominal_code = _invoice_line_nominal_source(
            product=resolved_product,
            snapshot=snapshot,
            requested_acctnum=requested_acctnum,
        )
        source_key = (
            product_id,
            product_code,
            product_description,
            nominal_source,
            nominal_code,
        )
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        product_sources.append(
            {
                "product_id": product_id,
                "product_code": product_code,
                "product_description": product_description,
                "nominal_source": nominal_source,
                "nominal_code": nominal_code,
            }
        )

    return {
        "invoice_id": int(invoice_id),
        "requested_acctnum": requested_acctnum,
        "product_sources": product_sources,
    }


def _display_label(value: object) -> str:
    text = str(value or "").strip().replace("_", " ")
    return text.title() if text else "-"


def _job_label(job: AccountingSyncJob) -> str:
    return _display_label(job.job_type)


def _job_entity_label(job: AccountingSyncJob) -> str:
    entity_type = _display_label(job.entity_type)
    entity_id = int(getattr(job, "entity_id", 0) or 0)
    return f"{entity_type} {entity_id}" if entity_id > 0 else entity_type


def _setup_attention_rows(
    *,
    connection: AccountingConnection | None,
    setup_summary: object | None,
) -> list[dict[str, object]]:
    setup = setup_summary
    if setup is None:
        return []
    rows: list[dict[str, object]] = []
    connection_status = str(getattr(connection, "status", "") or "").strip().lower()
    if connection_status != "connected":
        rows.append(
            {
                "category_label": "Setup required",
                "entity_label": "QuickBooks connection",
                "reason_text": "QuickBooks is not connected for this tenant.",
                "next_action_hint": "Connect or reconnect QuickBooks before running sync jobs.",
                "retry_guidance": "No",
                "sort_order": 0,
            }
        )
    missing_tax_mapping_count = int(getattr(setup, "missing_tax_mapping_count", 0) or 0)
    if missing_tax_mapping_count > 0:
        rows.append(
            {
                "category_label": "Setup required",
                "entity_label": "Tax mappings",
                "reason_text": f"{missing_tax_mapping_count} required local tax rate(s) still need QuickBooks mappings.",
                "next_action_hint": "Open Manage Tax Mappings and save the missing QuickBooks mappings.",
                "retry_guidance": "After fix",
                "sort_order": 1,
            }
        )
    products_missing_tax_rate = int(getattr(setup, "products_missing_tax_rate", 0) or 0)
    if products_missing_tax_rate > 0:
        rows.append(
            {
                "category_label": "Setup required",
                "entity_label": "Products",
                "reason_text": f"{products_missing_tax_rate} product(s) do not have a local tax rate.",
                "next_action_hint": "Add a local tax rate to each affected product before syncing invoices.",
                "retry_guidance": "After fix",
                "sort_order": 2,
            }
        )
    products_missing_nominal_code = int(getattr(setup, "products_missing_nominal_code", 0) or 0)
    has_default_revenue_account_mapping = bool(
        getattr(setup, "has_default_revenue_account_mapping", False)
    )
    if products_missing_nominal_code > 0 and not has_default_revenue_account_mapping:
        rows.append(
            {
                "category_label": "Setup required",
                "entity_label": "Revenue account setup",
                "reason_text": (
                    f"{products_missing_nominal_code} product(s) have no nominal fallback and no default revenue account is saved."
                ),
                "next_action_hint": "Save a default revenue account or add product nominal codes before retrying.",
                "retry_guidance": "After fix",
                "sort_order": 3,
            }
        )
    return rows


def _job_failure_guidance(
    job: AccountingSyncJob,
    *,
    account_mismatch: dict[str, object] | None,
) -> dict[str, object]:
    error_text = _normalize_text(job.error_text) or "Accounting sync failed."
    normalized_error = error_text.lower()
    if account_mismatch is not None:
        requested_acctnum = str(account_mismatch.get("requested_acctnum") or "").strip()
        return {
            "category_label": "Setup required",
            "reason_text": (
                f"Invoice still expects AcctNum {requested_acctnum}."
                if requested_acctnum
                else error_text
            ),
            "next_action_hint": (
                "Update the product or revenue-account setup so the invoice resolves to the correct QuickBooks income account. "
                "Retrying the same failed job keeps the same invoice context; create a fresh invoice if needed."
            ),
            "retry_guidance": "After fix",
            "sort_order": 10,
        }
    if "no quickbooks tax mapping" in normalized_error:
        return {
            "category_label": "Setup required",
            "reason_text": error_text,
            "next_action_hint": "Save the missing QuickBooks tax mapping, then retry this job.",
            "retry_guidance": "After fix",
            "sort_order": 11,
        }
    if (
        "display code/label" in normalized_error
        or "provider ref" in normalized_error
        or "re-save this mapping" in normalized_error
    ):
        return {
            "category_label": "Setup required",
            "reason_text": error_text,
            "next_action_hint": "Re-save the affected tax mapping from the QuickBooks list, then retry.",
            "retry_guidance": "After fix",
            "sort_order": 12,
        }
    if (
        "no default revenue account is selected" in normalized_error
        or "nominal code fallback" in normalized_error
        or "income account with acctnum" in normalized_error
        or "configured default quickbooks revenue account is invalid" in normalized_error
        or "quickbooks connection is not active" in normalized_error
        or "missing snapshotted tax rate data" in normalized_error
    ):
        return {
            "category_label": "Setup required",
            "reason_text": error_text,
            "next_action_hint": "Fix the tenant setup for this record, then retry the failed job.",
            "retry_guidance": "After fix",
            "sort_order": 13,
        }
    if (
        "invoice total does not match the local invoice gross total" in normalized_error
        or "invoice tax total does not match the local invoice tax total" in normalized_error
        or "invoice gross total does not match its local invoice lines" in normalized_error
        or "invoice tax total does not match its local invoice lines" in normalized_error
        or "invoice net total does not match its local invoice lines" in normalized_error
    ):
        return {
            "category_label": "Manual review required",
            "reason_text": error_text,
            "next_action_hint": (
                "Compare the local invoice with the remote QuickBooks invoice before retrying. "
                "This is not an ordinary retry-only failure."
            ),
            "retry_guidance": "After review",
            "sort_order": 20,
        }
    if str(job.job_type or "").strip().lower() == "mark_invoice_paid" and "not found" in normalized_error:
        return {
            "category_label": "Setup required",
            "reason_text": error_text,
            "next_action_hint": "Make sure invoice sync has succeeded first, then retry the payment job.",
            "retry_guidance": "After fix",
            "sort_order": 14,
        }
    return {
        "category_label": "Retryable failure",
        "reason_text": error_text,
        "next_action_hint": "Retry the failed job. If it fails again, review the recent activity and provider error details.",
        "retry_guidance": "Yes",
        "sort_order": 30,
    }


def _account_mismatch_detail_lines(account_mismatch: dict[str, object] | None) -> list[str]:
    if not isinstance(account_mismatch, dict):
        return []
    lines: list[str] = []
    for source in account_mismatch.get("product_sources", []):
        if not isinstance(source, dict):
            continue
        lines.append(
            (
                f"Product {source['product_id']}" if source.get("product_id") else "Product"
            )
            + (f" {source['product_code']}" if source.get("product_code") else "")
            + (
                f" - {source['product_description']}"
                if source.get("product_description")
                else ""
            )
            + (
                f" ({source['nominal_source']}: {source['nominal_code']})"
                if source.get("nominal_code")
                else f" ({source['nominal_source']})"
            )
        )
    return lines


def _recent_job_row(
    db: Session,
    *,
    tenant_id: int,
    job: AccountingSyncJob,
) -> dict[str, object]:
    account_mismatch = (
        _invoice_account_mismatch_context(
            db,
            tenant_id=int(tenant_id),
            invoice_id=int(job.entity_id),
            error_text=job.error_text,
        )
        if str(job.status or "").strip().lower() == "failed"
        and str(job.job_type or "").strip().lower() == "sync_invoice"
        and str(job.entity_type or "").strip().lower() == "invoice"
        else None
    )
    status_label = _display_label(job.status)
    category_label = None
    reason_text = None
    next_action_hint = None
    retry_guidance = "-"
    sort_order = 999
    if str(job.status or "").strip().lower() == "failed":
        guidance = _job_failure_guidance(job, account_mismatch=account_mismatch)
        category_label = str(guidance["category_label"])
        reason_text = str(guidance["reason_text"])
        next_action_hint = str(guidance["next_action_hint"])
        retry_guidance = str(guidance["retry_guidance"])
        sort_order = int(guidance["sort_order"])
    elif str(job.status or "").strip().lower() == "succeeded":
        category_label = "Resolved / succeeded"
        reason_text = "Job completed successfully."
        sort_order = 1000
    return {
        "id": job.id,
        "job_type": job.job_type,
        "job_label": _job_label(job),
        "entity_type": job.entity_type,
        "entity_id": job.entity_id,
        "entity_label": _job_entity_label(job),
        "status": job.status,
        "status_label": status_label,
        "attempts": job.attempts,
        "updated_at": job.updated_at,
        "error_text": job.error_text,
        "category_label": category_label,
        "reason_text": reason_text,
        "next_action_hint": next_action_hint,
        "retry_guidance": retry_guidance,
        "sort_order": sort_order,
        "account_mismatch": account_mismatch,
        "account_mismatch_detail_lines": _account_mismatch_detail_lines(account_mismatch),
    }


def _needs_attention_rows(
    *,
    connection: AccountingConnection | None,
    setup_summary: object | None,
    recent_jobs: list[dict[str, object]],
) -> list[dict[str, object]]:
    setup_rows = _setup_attention_rows(
        connection=connection,
        setup_summary=setup_summary,
    )
    failed_job_rows = [
        {
            "category_label": str(job.get("category_label") or ""),
            "entity_label": str(job.get("entity_label") or ""),
            "reason_text": str(job.get("reason_text") or ""),
            "next_action_hint": str(job.get("next_action_hint") or ""),
            "retry_guidance": str(job.get("retry_guidance") or "-"),
            "sort_order": int(job.get("sort_order") or 999),
        }
        for job in recent_jobs
        if str(job.get("status") or "").strip().lower() == "failed"
    ]
    attention_rows = setup_rows + failed_job_rows
    attention_rows.sort(
        key=lambda row: (
            int(row.get("sort_order") or 999),
            str(row.get("entity_label") or "").lower(),
            str(row.get("reason_text") or "").lower(),
        )
    )
    return attention_rows[:8]


def _recent_event_row(event: AccountingSyncEvent) -> dict[str, object]:
    entity_type = _normalize_text(getattr(event, "entity_type", None))
    entity_id = int(getattr(event, "entity_id", 0) or 0)
    entity_label = None
    if entity_type:
        entity_label = (
            f"{_display_label(entity_type)} {entity_id}"
            if entity_id > 0
            else _display_label(entity_type)
        )
    return {
        "created_at": event.created_at,
        "event_label": _display_label(event.event_type),
        "entity_label": entity_label,
        "summary": str(event.summary or "").strip() or None,
    }


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


def _revenue_account_redirect(
    *,
    saved: bool = False,
    cleared: bool = False,
    error: str = "",
) -> RedirectResponse:
    query: dict[str, str] = {}
    if saved:
        query["revenue_account_saved"] = "1"
    if cleared:
        query["revenue_account_cleared"] = "1"
    if error:
        query["error"] = error
    url = "/admin/accounting"
    if query:
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url=url, status_code=303)


def _page_context(
    request: Request,
    *,
    connection: AccountingConnection | None,
    recent_jobs: list[AccountingSyncJob] | None = None,
    recent_events: list[AccountingSyncEvent] | None = None,
    needs_attention_rows: list[dict[str, object]] | None = None,
    setup_summary: object | None = None,
    config_error: str = "",
    revenue_account_options: list[object] | None = None,
    current_revenue_account_mapping: object | None = None,
    suggested_revenue_account_id: str | None = None,
    revenue_account_error: str = "",
    tax_discovery: object | None = None,
    tax_code_options: list[object] | None = None,
    tax_code_error: str = "",
) -> dict[str, object]:
    return {
        "request": request,
        "connection": connection,
        "recent_jobs": recent_jobs or [],
        "recent_events": recent_events or [],
        "needs_attention_rows": needs_attention_rows or [],
        "config_error": config_error,
        "revenue_account_options": revenue_account_options or [],
        "current_revenue_account_mapping": current_revenue_account_mapping,
        "suggested_revenue_account_id": suggested_revenue_account_id,
        "revenue_account_error": revenue_account_error,
        "tax_discovery": tax_discovery,
        "tax_code_options": tax_code_options or [],
        "tax_code_option_ids": {
            str(getattr(option, "remote_tax_code_id", "") or "").strip()
            for option in (tax_code_options or [])
        },
        "tax_code_error": tax_code_error,
        "quickbooks_connected": _query_flag(request, "quickbooks_connected"),
        "quickbooks_disconnected": _query_flag(request, "quickbooks_disconnected"),
        "tax_mapping_saved": _query_flag(request, "tax_mapping_saved"),
        "tax_mapping_deleted": _query_flag(request, "tax_mapping_deleted"),
        "revenue_account_saved": _query_flag(request, "revenue_account_saved"),
        "revenue_account_cleared": _query_flag(request, "revenue_account_cleared"),
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
    current_revenue_account_mapping = get_default_revenue_account_mapping(
        db,
        tenant_id=int(tenant_id),
        provider=QUICKBOOKS_PROVIDER,
    )
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
    recent_job_rows = [
        _recent_job_row(
            db,
            tenant_id=int(tenant_id),
            job=job,
        )
        for job in recent_jobs
    ]
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
    recent_event_rows = [_recent_event_row(event) for event in recent_events]
    config_error = ""
    try:
        redirect_uri = resolve_quickbooks_redirect_uri(request)
        if not str(settings.quickbooks_client_secret or "").strip():
            raise QuickBooksOAuthError("QUICKBOOKS_CLIENT_SECRET is not configured.")
        build_quickbooks_authorize_url(state="preview", redirect_uri=redirect_uri)
    except QuickBooksOAuthError as exc:
        config_error = str(exc)
    revenue_account_options: list[dict[str, object]] = []
    suggested_revenue_account_id: str | None = None
    revenue_account_error = ""
    if connection is not None and str(connection.status or "").strip().lower() == "connected":
        try:
            revenue_account_options = _revenue_account_option_rows(
                list_provider_revenue_accounts(
                    db,
                    tenant_id=int(tenant_id),
                    provider=QUICKBOOKS_PROVIDER,
                )
            )
            suggested_revenue_account_id = _suggested_revenue_account_id(
                revenue_account_options
            )
        except (RevenueAccountMappingValidationError, QuickBooksApiError) as exc:
            revenue_account_error = str(exc)
    return templates.TemplateResponse(
        request,
        "admin/accounting/index.html",
        _page_context(
            request,
            connection=connection,
            recent_jobs=recent_job_rows,
            recent_events=recent_event_rows,
            needs_attention_rows=_needs_attention_rows(
                connection=connection,
                setup_summary=setup_summary,
                recent_jobs=recent_job_rows,
            ),
            setup_summary=setup_summary,
            config_error=config_error,
            revenue_account_options=revenue_account_options,
            current_revenue_account_mapping=current_revenue_account_mapping,
            suggested_revenue_account_id=suggested_revenue_account_id,
            revenue_account_error=revenue_account_error,
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
    tax_discovery = None
    tax_code_options: list[object] = []
    tax_code_error = ""
    if connection is not None and str(connection.status or "").strip().lower() == "connected":
        try:
            tax_code_options = list_provider_tax_codes(
                db,
                tenant_id=int(tenant_id),
                provider=QUICKBOOKS_PROVIDER,
            )
        except (TaxMappingValidationError, QuickBooksApiError) as exc:
            tax_code_error = str(exc)
        try:
            tax_discovery = inspect_quickbooks_tax_discovery(
                db,
                tenant_id=int(tenant_id),
                provider=QUICKBOOKS_PROVIDER,
            )
        except (TaxMappingValidationError, QuickBooksApiError):
            tax_discovery = None
    setup_summary = summarize_quickbooks_setup(
        db,
        tenant_id=tenant_id,
        connection_status=getattr(connection, "status", None),
        provider_tax_code_options=(
            tax_code_options if not tax_code_error else None
        ),
    )
    return templates.TemplateResponse(
        request,
        "admin/accounting/tax_mappings.html",
        _page_context(
            request,
            connection=connection,
            setup_summary=setup_summary,
            tax_discovery=tax_discovery,
            tax_code_options=tax_code_options,
            tax_code_error=tax_code_error,
        ),
    )


@router.post("/admin/accounting/revenue-account")
async def admin_accounting_revenue_account_update(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_tenant_accounting_admin(request, db)
    tenant_id = request_tenant_id(request)
    form = await request.form()
    remote_account_id = str(form.get("remote_account_id", "") or "").strip()
    try:
        if remote_account_id:
            save_default_revenue_account_mapping(
                db,
                tenant_id=int(tenant_id),
                provider=QUICKBOOKS_PROVIDER,
                remote_account_id=remote_account_id,
            )
            db.commit()
            return _revenue_account_redirect(saved=True)
        clear_default_revenue_account_mapping(
            db,
            tenant_id=int(tenant_id),
            provider=QUICKBOOKS_PROVIDER,
        )
        db.commit()
    except (RevenueAccountMappingValidationError, QuickBooksApiError) as exc:
        db.rollback()
        return _revenue_account_redirect(error=str(exc))
    return _revenue_account_redirect(saved=True, cleared=True)


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
