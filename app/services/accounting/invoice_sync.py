from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    AccountingInvoiceSync,
    Invoice,
    InvoiceLine,
    Product,
    Ticket,
)
from ...models.base import utcnow
from .customer_sync import sync_customer_to_quickbooks
from .jobs import get_active_accounting_connection, log_accounting_event
from .product_sync import sync_product_to_quickbooks
from .quickbooks_client import (
    QuickBooksApiError,
    QuickBooksUnsupportedError,
    compact_quickbooks_entity,
    quickbooks_client_for_connection,
    quote_query_value,
)
from .quickbooks_oauth import QUICKBOOKS_PROVIDER
from .tax_mapping import QuickBooksTaxSelection, require_quickbooks_tax_selection


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _money(value: object) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _quantity(value: object) -> float:
    quantity = Decimal(str(value or 0))
    if quantity <= 0:
        return 1.0
    return float(quantity.quantize(Decimal("0.001")))


def _decimal_money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _invoice_for_sync(db: Session, *, tenant_id: int, invoice_id: int) -> Invoice:
    invoice = (
        db.execute(
            select(Invoice).where(
                Invoice.id == int(invoice_id),
                Invoice.tenant_id == int(tenant_id),
            )
        )
        .scalars()
        .first()
    )
    if invoice is None:
        raise QuickBooksApiError("Invoice was not found for accounting sync.")
    return invoice


def _invoice_line_rows(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
) -> list[tuple[InvoiceLine, Ticket | None, Product | None]]:
    return list(
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
                InvoiceLine.invoice_id == int(invoice_id),
                InvoiceLine.tenant_id == int(tenant_id),
            )
            .order_by(InvoiceLine.id.asc())
        ).all()
    )


def _snapshot_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _snapshot_int(snapshot: dict[str, Any], key: str) -> int | None:
    raw = snapshot.get(key)
    try:
        resolved = int(raw)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _invoice_line_product_id(
    line: InvoiceLine,
    *,
    ticket: Ticket | None,
    product: Product | None,
) -> int | None:
    snapshot = _snapshot_dict(line.product_snapshot_json)
    snapshot_product_id = _snapshot_int(snapshot, "product_id")
    if snapshot_product_id:
        return snapshot_product_id
    if product is not None and product.id is not None:
        return int(product.id)
    if ticket is not None and ticket.product_id is not None:
        return int(ticket.product_id)
    return None


def _invoice_line_tax_rate_id(line: InvoiceLine) -> int:
    snapshot = _snapshot_dict(line.product_snapshot_json)
    snapshot_tax_rate_id = _snapshot_int(snapshot, "tax_rate_id")
    if snapshot_tax_rate_id:
        return snapshot_tax_rate_id
    raise QuickBooksApiError(
        f"Invoice line {line.id} is missing snapshotted tax rate data for QuickBooks sync."
    )


def _validate_invoice_totals(
    invoice: Invoice,
    *,
    line_rows: list[tuple[InvoiceLine, Ticket | None, Product | None]],
) -> None:
    net_total = Decimal("0.00")
    vat_total = Decimal("0.00")
    gross_total = Decimal("0.00")
    for line, _ticket, _product in line_rows:
        line_net = _decimal_money(line.net)
        line_vat = _decimal_money(line.vat)
        line_gross = _decimal_money(line.gross)
        if line_gross != (line_net + line_vat):
            raise QuickBooksApiError(
                f"Invoice line {line.id} totals are inconsistent locally (gross != net + tax)."
            )
        net_total += line_net
        vat_total += line_vat
        gross_total += line_gross

    if net_total != _decimal_money(invoice.net_total):
        raise QuickBooksApiError("Invoice net total does not match its local invoice lines.")
    if vat_total != _decimal_money(invoice.vat_total):
        raise QuickBooksApiError("Invoice tax total does not match its local invoice lines.")
    if gross_total != _decimal_money(invoice.gross_total):
        raise QuickBooksApiError("Invoice gross total does not match its local invoice lines.")


def get_or_create_accounting_invoice_sync(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingInvoiceSync:
    sync_row = (
        db.execute(
            select(AccountingInvoiceSync).where(
                AccountingInvoiceSync.tenant_id == int(tenant_id),
                AccountingInvoiceSync.provider == str(provider or "").strip().lower(),
                AccountingInvoiceSync.invoice_id == int(invoice_id),
            )
        )
        .scalars()
        .first()
    )
    if sync_row is not None:
        return sync_row
    sync_row = AccountingInvoiceSync(
        tenant_id=int(tenant_id),
        provider=str(provider or "").strip().lower(),
        invoice_id=int(invoice_id),
        sync_status="pending",
    )
    db.add(sync_row)
    db.flush()
    return sync_row


def mark_invoice_sync_failed(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    message: str,
    provider: str = QUICKBOOKS_PROVIDER,
) -> AccountingInvoiceSync:
    sync_row = get_or_create_accounting_invoice_sync(
        db,
        tenant_id=int(tenant_id),
        invoice_id=int(invoice_id),
        provider=provider,
    )
    sync_row.sync_status = "failed"
    sync_row.last_error = str(message or "").strip() or None
    sync_row.updated_at = utcnow()
    return sync_row


def _invoice_payload(
    invoice: Invoice,
    *,
    customer_external_id: str,
    line_items: list[dict[str, Any]],
    txn_tax_code_ref: str | None,
    global_tax_calculation: str | None,
) -> dict[str, Any]:
    payload = {
        "CustomerRef": {"value": str(customer_external_id)},
        "DocNumber": str(invoice.invoice_no or "").strip(),
        "TxnDate": invoice.invoice_date.isoformat(),
        "Line": line_items,
    }
    if txn_tax_code_ref:
        payload["TxnTaxDetail"] = {"TxnTaxCodeRef": {"value": str(txn_tax_code_ref)}}
    if global_tax_calculation:
        payload["GlobalTaxCalculation"] = str(global_tax_calculation)
    if invoice.due_date is not None:
        payload["DueDate"] = invoice.due_date.isoformat()
    return payload


def _invoice_line_payload(
    line: InvoiceLine,
    *,
    item_external_id: str,
    tax_selection: QuickBooksTaxSelection,
) -> dict[str, Any]:
    amount = _money(line.net)
    qty = _quantity(line.quantity)
    if line.unit_price not in (None, ""):
        unit_price = _money(line.unit_price)
    else:
        unit_price = round(amount / qty, 2) if qty > 0 else amount
    return {
        "Amount": amount,
        "Description": str(line.description or "").strip() or None,
        "DetailType": "SalesItemLineDetail",
        "SalesItemLineDetail": {
            "ItemRef": {"value": str(item_external_id)},
            "Qty": qty,
            "UnitPrice": unit_price,
            "TaxCodeRef": {"value": tax_selection.line_tax_code_ref},
        },
    }


def _build_invoice_line_items(
    db: Session,
    *,
    tenant_id: int,
    provider: str,
    line_rows: list[tuple[InvoiceLine, Ticket | None, Product | None]],
) -> tuple[list[dict[str, Any]], str | None, str | None, list[dict[str, Any]]]:
    product_external_ids: dict[int, str] = {}
    line_items: list[dict[str, Any]] = []
    tax_details: list[dict[str, Any]] = []
    pseudo_mode: bool | None = None
    txn_tax_code_refs: set[str] = set()

    for line, ticket, product in line_rows:
        product_id = _invoice_line_product_id(line, ticket=ticket, product=product)
        if product_id is None:
            raise QuickBooksApiError(
                f"Invoice line {line.id} is missing a linked product for QuickBooks sync."
            )

        if product_id not in product_external_ids:
            product_sync = sync_product_to_quickbooks(
                db,
                tenant_id=int(tenant_id),
                product_id=int(product_id),
                provider=provider,
            )
            product_external_ids[product_id] = str(product_sync["external_id"])

        tax_selection = require_quickbooks_tax_selection(
            db,
            tenant_id=int(tenant_id),
            provider=provider,
            tax_rate_id=_invoice_line_tax_rate_id(line),
            usage_label=f"Invoice line {line.id}",
        )
        if pseudo_mode is None:
            pseudo_mode = tax_selection.uses_pseudo_line_code
        elif pseudo_mode != tax_selection.uses_pseudo_line_code:
            raise QuickBooksApiError(
                "Invoice mixes incompatible QuickBooks tax mapping styles; use either pseudo TAX/NON mappings or direct tax-code mappings on all lines."
            )

        if tax_selection.txn_tax_code_ref:
            txn_tax_code_refs.add(str(tax_selection.txn_tax_code_ref))

        line_items.append(
            _invoice_line_payload(
                line,
                item_external_id=product_external_ids[product_id],
                tax_selection=tax_selection,
            )
        )
        tax_details.append(
            {
                "invoice_line_id": int(line.id),
                "product_id": int(product_id),
                "tax_rate_id": int(tax_selection.tax_rate_id),
                "tax_map_id": int(tax_selection.tax_map_id),
                "local_tax_label": tax_selection.local_tax_label,
                "line_tax_code_ref": tax_selection.line_tax_code_ref,
                "txn_tax_code_ref": tax_selection.txn_tax_code_ref,
                "line_net": _money(line.net),
                "line_tax": _money(line.vat),
                "line_gross": _money(line.gross),
            }
        )

    txn_tax_code_ref: str | None = None
    global_tax_calculation: str | None = None
    if pseudo_mode:
        if len(txn_tax_code_refs) > 1:
            raise QuickBooksApiError(
                "QuickBooks invoice sync found multiple invoice-level tax code mappings on one invoice."
            )
        if any(_decimal_money(line.vat) > Decimal("0.00") for line, _ticket, _product in line_rows):
            txn_tax_code_ref = next(iter(txn_tax_code_refs), None)
            if not txn_tax_code_ref:
                raise QuickBooksApiError(
                    "QuickBooks invoice sync is missing an invoice-level tax code/group mapping for taxable lines."
                )
    else:
        global_tax_calculation = "TaxExcluded"

    return line_items, txn_tax_code_ref, global_tax_calculation, tax_details


def _validate_remote_invoice_totals(invoice: Invoice, remote_invoice: dict[str, Any]) -> None:
    remote_total_raw = remote_invoice.get("TotalAmt")
    remote_tax_raw = None
    txn_tax_detail = remote_invoice.get("TxnTaxDetail")
    if isinstance(txn_tax_detail, dict):
        remote_tax_raw = txn_tax_detail.get("TotalTax")

    if remote_total_raw not in (None, ""):
        remote_total = _decimal_money(remote_total_raw)
        if remote_total != _decimal_money(invoice.gross_total):
            raise QuickBooksApiError(
                "QuickBooks invoice total does not match the local invoice gross total."
            )
    if remote_tax_raw not in (None, ""):
        remote_tax = _decimal_money(remote_tax_raw)
        if remote_tax != _decimal_money(invoice.vat_total):
            raise QuickBooksApiError(
                "QuickBooks invoice tax total does not match the local invoice tax total."
            )


def sync_invoice_to_quickbooks(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> dict[str, Any]:
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        raise QuickBooksApiError("QuickBooks connection is not active for this tenant.")

    invoice = _invoice_for_sync(db, tenant_id=int(tenant_id), invoice_id=int(invoice_id))
    sync_row = get_or_create_accounting_invoice_sync(
        db,
        tenant_id=int(tenant_id),
        invoice_id=int(invoice_id),
        provider=provider,
    )
    if str(sync_row.external_id or "").strip():
        return {
            "external_id": str(sync_row.external_id),
            "status": "already_synced",
            "external_doc_number": str(sync_row.external_doc_number or "").strip() or None,
        }

    client = quickbooks_client_for_connection(db, connection)
    customer_sync = sync_customer_to_quickbooks(
        db,
        tenant_id=int(tenant_id),
        customer_id=int(invoice.customer_id),
        provider=provider,
    )
    line_rows = _invoice_line_rows(db, tenant_id=int(tenant_id), invoice_id=int(invoice.id))
    if not line_rows:
        raise QuickBooksApiError("Invoice has no lines to sync to QuickBooks.")
    _validate_invoice_totals(invoice, line_rows=line_rows)
    line_items, txn_tax_code_ref, global_tax_calculation, tax_details = _build_invoice_line_items(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        line_rows=line_rows,
    )

    payload = _invoice_payload(
        invoice,
        customer_external_id=str(customer_sync["external_id"]),
        line_items=line_items,
        txn_tax_code_ref=txn_tax_code_ref,
        global_tax_calculation=global_tax_calculation,
    )
    payload_hash = _payload_hash(payload)

    matches = client.query_entities(
        "invoice",
        f"SELECT * FROM Invoice WHERE DocNumber = '{quote_query_value(invoice.invoice_no)}'",
    )
    if matches:
        remote_invoice = matches[0]
        response_status_code = 200
    else:
        result = client.create_entity("invoice", payload)
        remote_invoice = result.payload
        response_status_code = result.status_code

    _validate_remote_invoice_totals(invoice, remote_invoice)

    external_id = str(remote_invoice.get("Id") or "").strip()
    if not external_id:
        raise QuickBooksApiError("QuickBooks invoice response did not include an Id.")

    sync_row.external_id = external_id
    sync_row.external_doc_number = (
        str(remote_invoice.get("DocNumber") or "").strip() or str(invoice.invoice_no or "")
    )
    sync_row.sync_status = "invoice_synced"
    sync_row.last_synced_at = utcnow()
    sync_row.last_error = None
    sync_row.payload_hash = payload_hash
    sync_row.provider_response_json = {
        "operation": "sync_invoice",
        "response_status_code": response_status_code,
        "invoice": compact_quickbooks_entity(remote_invoice),
        "line_amount_basis": "net_exclusive",
        "global_tax_calculation": global_tax_calculation,
        "txn_tax_code_ref": txn_tax_code_ref,
        "local_totals": {
            "net_total": _money(invoice.net_total),
            "tax_total": _money(invoice.vat_total),
            "gross_total": _money(invoice.gross_total),
        },
        "tax_details": tax_details,
    }

    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="invoice_synced",
        entity_type="invoice",
        entity_id=int(invoice.id),
        direction="OUTBOUND",
        summary="Synced invoice to QuickBooks",
        detail_json={
            "external_id": external_id,
            "external_doc_number": sync_row.external_doc_number,
            "response_status_code": response_status_code,
        },
    )
    return {
        "external_id": external_id,
        "external_doc_number": sync_row.external_doc_number,
        "status": "invoice_synced",
    }


def sync_invoice_payment_to_quickbooks(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> dict[str, Any]:
    connection = get_active_accounting_connection(
        db,
        int(tenant_id),
        provider=provider,
    )
    if connection is None:
        raise QuickBooksApiError("QuickBooks connection is not active for this tenant.")

    invoice = _invoice_for_sync(db, tenant_id=int(tenant_id), invoice_id=int(invoice_id))
    if str(invoice.status or "").strip().upper() != "PAID":
        raise QuickBooksApiError("Invoice is not marked paid locally.")

    sync_row = get_or_create_accounting_invoice_sync(
        db,
        tenant_id=int(tenant_id),
        invoice_id=int(invoice.id),
        provider=provider,
    )
    if (
        str(sync_row.sync_status or "").strip().lower() == "payment_synced"
        and str(sync_row.external_id or "").strip()
    ):
        return {
            "external_id": str(sync_row.external_id),
            "status": "already_paid_synced",
        }

    invoice_sync = sync_invoice_to_quickbooks(
        db,
        tenant_id=int(tenant_id),
        invoice_id=int(invoice.id),
        provider=provider,
    )
    customer_sync = sync_customer_to_quickbooks(
        db,
        tenant_id=int(tenant_id),
        customer_id=int(invoice.customer_id),
        provider=provider,
    )

    client = quickbooks_client_for_connection(db, connection)
    remote_invoice = client.get_entity(
        "invoice",
        str(invoice_sync["external_id"]),
        allow_not_found=False,
    )
    balance = Decimal(str(remote_invoice.get("Balance") or 0))
    if balance <= Decimal("0"):
        sync_row.external_id = str(invoice_sync["external_id"])
        sync_row.external_doc_number = (
            str(invoice_sync.get("external_doc_number") or "") or sync_row.external_doc_number
        )
        sync_row.sync_status = "payment_synced"
        sync_row.last_synced_at = utcnow()
        sync_row.last_error = None
        sync_row.provider_response_json = {
            "operation": "mark_invoice_paid",
            "remote_invoice": compact_quickbooks_entity(remote_invoice),
            "payment": {"status": "already_paid"},
        }
        log_accounting_event(
            db,
            tenant_id=int(tenant_id),
            provider=provider,
            event_type="invoice_payment_synced",
            entity_type="invoice",
            entity_id=int(invoice.id),
            direction="OUTBOUND",
            summary="QuickBooks invoice was already fully paid",
            detail_json={
                "external_id": str(sync_row.external_id or ""),
                "balance": float(balance),
            },
        )
        return {
            "external_id": str(sync_row.external_id),
            "status": "already_paid",
        }

    payment_payload = {
        "CustomerRef": {"value": str(customer_sync["external_id"])},
        "TotalAmt": _money(invoice.gross_total),
        "TxnDate": (invoice.paid_at.date() if invoice.paid_at else invoice.invoice_date).isoformat(),
        "Line": [
            {
                "Amount": _money(invoice.gross_total),
                "LinkedTxn": [
                    {
                        "TxnId": str(invoice_sync["external_id"]),
                        "TxnType": "Invoice",
                    }
                ],
            }
        ],
    }
    result = client.create_payment(payment_payload)
    payment = result.payload

    sync_row.external_id = str(invoice_sync["external_id"])
    sync_row.external_doc_number = (
        str(invoice_sync.get("external_doc_number") or "") or sync_row.external_doc_number
    )
    sync_row.sync_status = "payment_synced"
    sync_row.last_synced_at = utcnow()
    sync_row.last_error = None
    sync_row.provider_response_json = {
        "operation": "mark_invoice_paid",
        "response_status_code": result.status_code,
        "payment": compact_quickbooks_entity(payment),
    }
    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="invoice_payment_synced",
        entity_type="invoice",
        entity_id=int(invoice.id),
        direction="OUTBOUND",
        summary="Synced invoice payment to QuickBooks",
        detail_json={
            "external_id": str(sync_row.external_id or ""),
            "payment_id": str(payment.get("Id") or "").strip() or None,
            "response_status_code": result.status_code,
        },
    )
    return {
        "external_id": str(sync_row.external_id),
        "payment_id": str(payment.get("Id") or "").strip() or None,
        "status": "payment_synced",
    }


def sync_invoice_void_to_quickbooks(
    db: Session,
    *,
    tenant_id: int,
    invoice_id: int,
    provider: str = QUICKBOOKS_PROVIDER,
) -> dict[str, Any]:
    log_accounting_event(
        db,
        tenant_id=int(tenant_id),
        provider=provider,
        event_type="invoice_void_unsupported",
        entity_type="invoice",
        entity_id=int(invoice_id),
        direction="OUTBOUND",
        summary="QuickBooks invoice void sync is not supported yet",
        detail_json={"invoice_id": int(invoice_id)},
    )
    raise QuickBooksUnsupportedError("QuickBooks invoice void sync is not supported yet.")
