from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ...models import AccountingProductMap, Product
from ...models.base import utcnow
from ..pricing import product_effective_nominal_code
from .jobs import get_active_accounting_connection, log_accounting_event
from .quickbooks_client import (
    QuickBooksApiError,
    compact_quickbooks_entity,
    quickbooks_client_for_connection,
    quote_query_value,
)
from .quickbooks_oauth import QUICKBOOKS_PROVIDER
from .revenue_account_mapping import resolve_revenue_account
from .tax_mapping import require_quickbooks_tax_selection


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


def _product_for_sync(db: Session, *, tenant_id: int, product_id: int) -> Product:
    product = (
        db.execute(
            select(Product)
            .options(
                joinedload(Product.tax_rate),
                joinedload(Product.product_group),
            )
            .where(
                Product.id == int(product_id),
                Product.tenant_id == int(tenant_id),
            )
        )
        .scalars()
        .first()
    )
    if product is None:
        raise QuickBooksApiError("Product was not found for accounting sync.")
    return product


def _product_map(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    provider: str,
) -> AccountingProductMap | None:
    return (
        db.execute(
            select(AccountingProductMap).where(
                AccountingProductMap.tenant_id == int(tenant_id),
                AccountingProductMap.provider == str(provider or "").strip().lower(),
                AccountingProductMap.product_id == int(product_id),
            )
        )
        .scalars()
        .first()
    )


def _product_payload(
    product: Product,
    *,
    income_account_ref: str,
    is_taxable: bool,
    nominal_code: str,
) -> dict[str, Any]:
    description = str(product.description or "").strip()
    if nominal_code:
        description = f"{description} (Nominal: {nominal_code})"
    payload = {
        "Name": str(product.code or "").strip() or f"Product {product.id}",
        "Sku": str(product.code or "").strip() or None,
        "Description": description or None,
        "Type": "Service",
        "IncomeAccountRef": {"value": str(income_account_ref or "").strip()},
        "UnitPrice": float(product.unit_price or 0),
        "Taxable": bool(is_taxable),
        "Active": True,
    }
    return _clean_dict(payload)


def sync_product_to_quickbooks(
    db: Session,
    *,
    tenant_id: int,
    product_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> dict[str, Any]:
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        raise QuickBooksApiError("QuickBooks connection is not active for this tenant.")

    product = _product_for_sync(db, tenant_id=int(tenant_id), product_id=int(product_id))
    product_label = str(product.code or "").strip() or f"Product {product.id}"
    nominal_code = str(product_effective_nominal_code(product) or "").strip()
    if product.tax_rate_id is None:
        raise QuickBooksApiError(
            f"Product {product_label} is missing a tax rate, so its QuickBooks tax handling cannot be resolved safely."
        )

    tax_selection = require_quickbooks_tax_selection(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        tax_rate_id=int(product.tax_rate_id),
        usage_label=f"Product {product_label}",
    )
    client = quickbooks_client_for_connection(db, connection)
    resolved_revenue_account = resolve_revenue_account(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        product_label=product_label,
        nominal_code=nominal_code or None,
        client=client,
    )
    payload = _product_payload(
        product,
        income_account_ref=resolved_revenue_account.remote_account_id,
        is_taxable=tax_selection.is_taxable,
        nominal_code=nominal_code,
    )
    payload_hash = _payload_hash(payload)
    mapping = _product_map(
        db,
        tenant_id=int(tenant_id),
        product_id=int(product_id),
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

    remote_item: dict[str, Any] | None = None
    if mapping is not None and str(mapping.external_id or "").strip():
        remote_item = client.get_entity(
            "item",
            str(mapping.external_id),
            allow_not_found=True,
        )

    if remote_item is None:
        matches = client.query_entities(
            "item",
            f"SELECT * FROM Item WHERE Name = '{quote_query_value(product.code)}'",
        )
        if matches:
            remote_item = matches[0]

    if remote_item is not None:
        update_payload = dict(payload)
        update_payload["Id"] = str(remote_item.get("Id") or "").strip()
        update_payload["SyncToken"] = str(remote_item.get("SyncToken") or "0").strip() or "0"
        update_payload["sparse"] = True
        result = client.update_entity("item", update_payload)
        synced_item = result.payload
        response_status_code = result.status_code
    else:
        result = client.create_entity("item", payload)
        synced_item = result.payload
        response_status_code = result.status_code

    external_id = str(synced_item.get("Id") or "").strip()
    if not external_id:
        raise QuickBooksApiError("QuickBooks item response did not include an Id.")

    if mapping is None:
        mapping = AccountingProductMap(
            tenant_id=int(tenant_id),
            provider=str(provider or "").strip().lower(),
            product_id=int(product.id),
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
        "nominal_code": nominal_code or None,
        "income_account_ref": payload["IncomeAccountRef"]["value"],
        "revenue_account_resolution_source": resolved_revenue_account.resolution_source,
        "revenue_account_mapping_id": resolved_revenue_account.mapping_id,
        "resolved_remote_account_id": resolved_revenue_account.remote_account_id,
        "resolved_remote_account_code": resolved_revenue_account.remote_account_code,
        "resolved_remote_account_name": resolved_revenue_account.remote_account_name,
        "resolved_remote_account_type": resolved_revenue_account.remote_account_type,
        "resolved_remote_account_detail_type": resolved_revenue_account.remote_account_detail_type,
        "tax_mapping_id": tax_selection.tax_map_id,
        "tax_provider_ref": tax_selection.stored_provider_ref,
        "tax_display_code": tax_selection.stored_display_code,
        "taxable": tax_selection.is_taxable,
        "product": compact_quickbooks_entity(synced_item),
    }
    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="product_synced",
        entity_type="product",
        entity_id=int(product.id),
        direction="OUTBOUND",
        summary="Synced product to QuickBooks",
        detail_json=detail_json,
    )
    return {
        "external_id": external_id,
        "status": "synced",
        "payload_hash": payload_hash,
        "response_status_code": response_status_code,
    }
