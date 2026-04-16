from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AccountingCustomerMap, Customer
from ...models.base import utcnow
from .jobs import get_active_accounting_connection, log_accounting_event
from .quickbooks_client import (
    QuickBooksApiError,
    compact_quickbooks_entity,
    quickbooks_client_for_connection,
    quote_query_value,
)
from .quickbooks_oauth import QUICKBOOKS_PROVIDER


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _clean_dict(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            nested = _clean_dict(value)
            if nested:
                cleaned[key] = nested
            continue
        if value in (None, ""):
            continue
        cleaned[key] = value
    return cleaned


def _customer_display_name(customer: Customer) -> str:
    account_code = str(customer.account_code or "").strip()
    name = str(customer.name or "").strip()
    if account_code and name:
        return f"{account_code} - {name}"
    return account_code or name or f"Customer {customer.id}"


def _customer_payload(customer: Customer) -> dict[str, Any]:
    notes_parts = []
    if str(customer.account_code or "").strip():
        notes_parts.append(f"Account code: {customer.account_code}")
    if str(customer.vat_number or "").strip():
        notes_parts.append(f"VAT: {customer.vat_number}")
    payload = {
        "DisplayName": _customer_display_name(customer),
        "CompanyName": str(customer.name or "").strip(),
        "PrintOnCheckName": str(customer.name or "").strip(),
        "PrimaryEmailAddr": {
            "Address": str(customer.invoice_email or "").strip(),
        },
        "PrimaryPhone": {
            "FreeFormNumber": str(customer.phone or "").strip(),
        },
        "BillAddr": {
            "Line1": str(customer.address_line1 or "").strip(),
            "Line2": str(customer.address_line2 or "").strip(),
            "City": str(customer.city or "").strip(),
            "PostalCode": str(customer.postcode or "").strip(),
            "Country": str(customer.country or "").strip(),
        },
        "Notes": " | ".join(part for part in notes_parts if part),
    }
    return _clean_dict(payload)


def _customer_for_sync(db: Session, *, tenant_id: int, customer_id: int) -> Customer:
    customer = (
        db.execute(
            select(Customer).where(
                Customer.id == int(customer_id),
                Customer.tenant_id == int(tenant_id),
            )
        )
        .scalars()
        .first()
    )
    if customer is None:
        raise QuickBooksApiError("Customer was not found for accounting sync.")
    return customer


def _customer_map(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    provider: str,
) -> AccountingCustomerMap | None:
    return (
        db.execute(
            select(AccountingCustomerMap).where(
                AccountingCustomerMap.tenant_id == int(tenant_id),
                AccountingCustomerMap.provider == str(provider or "").strip().lower(),
                AccountingCustomerMap.customer_id == int(customer_id),
            )
        )
        .scalars()
        .first()
    )


def sync_customer_to_quickbooks(
    db: Session,
    *,
    tenant_id: int,
    customer_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> dict[str, Any]:
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        raise QuickBooksApiError("QuickBooks connection is not active for this tenant.")

    customer = _customer_for_sync(db, tenant_id=int(tenant_id), customer_id=int(customer_id))
    client = quickbooks_client_for_connection(db, connection)
    payload = _customer_payload(customer)
    payload_hash = _payload_hash(payload)
    mapping = _customer_map(
        db,
        tenant_id=int(tenant_id),
        customer_id=int(customer_id),
        provider=provider,
    )

    if (
        mapping is not None
        and str(mapping.external_id or "").strip()
        and str(mapping.sync_status or "").strip().lower() == "synced"
        and str(mapping.payload_hash or "").strip() == payload_hash
    ):
        return {
            "external_id": str(mapping.external_id),
            "status": "unchanged",
            "payload_hash": payload_hash,
        }

    remote_customer: dict[str, Any] | None = None
    if mapping is not None and str(mapping.external_id or "").strip():
        remote_customer = client.get_entity(
            "customer",
            str(mapping.external_id),
            allow_not_found=True,
        )

    if remote_customer is None:
        display_name = _customer_display_name(customer)
        matches = client.query_entities(
            "customer",
            f"SELECT * FROM Customer WHERE DisplayName = '{quote_query_value(display_name)}'",
        )
        if matches:
            remote_customer = matches[0]

    if remote_customer is not None:
        update_payload = dict(payload)
        update_payload["Id"] = str(remote_customer.get("Id") or "").strip()
        update_payload["SyncToken"] = str(remote_customer.get("SyncToken") or "0").strip() or "0"
        update_payload["sparse"] = True
        result = client.update_entity("customer", update_payload)
        synced_customer = result.payload
        response_status_code = result.status_code
    else:
        result = client.create_entity("customer", payload)
        synced_customer = result.payload
        response_status_code = result.status_code

    external_id = str(synced_customer.get("Id") or "").strip()
    if not external_id:
        raise QuickBooksApiError("QuickBooks customer response did not include an Id.")

    if mapping is None:
        mapping = AccountingCustomerMap(
            tenant_id=int(tenant_id),
            provider=str(provider or "").strip().lower(),
            customer_id=int(customer.id),
            external_id=external_id,
            sync_status="synced",
        )
        db.add(mapping)

    mapping.external_id = external_id
    mapping.sync_status = "synced"
    mapping.last_synced_at = utcnow()
    mapping.last_error = None
    mapping.payload_hash = payload_hash

    detail_json = {
        "external_id": external_id,
        "response_status_code": response_status_code,
        "customer": compact_quickbooks_entity(synced_customer),
    }
    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="customer_synced",
        entity_type="customer",
        entity_id=int(customer.id),
        direction="OUTBOUND",
        summary="Synced customer to QuickBooks",
        detail_json=detail_json,
    )
    return {
        "external_id": external_id,
        "status": "synced",
        "payload_hash": payload_hash,
        "response_status_code": response_status_code,
    }
