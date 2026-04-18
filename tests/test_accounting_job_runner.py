import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select

import app.services.accounting.invoice_sync as invoice_sync_module
import app.services.accounting.quickbooks_client as quickbooks_client_module
import app.services.accounting.quickbooks_oauth as quickbooks_oauth_module
import app.services.accounting.tax_mapping as tax_mapping_module
from app.config import settings
from app.models import (
    AccountingConnection,
    AccountingCustomerMap,
    AccountingInvoiceSync,
    AccountingProductMap,
    AccountingRevenueAccountMap,
    AccountingSyncEvent,
    AccountingSyncJob,
    AccountingTaxMap,
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)
from app.services.accounting.job_runner import process_pending_accounting_jobs
from app.services.accounting.jobs import (
    enqueue_mark_invoice_paid,
    enqueue_sync_customer,
    enqueue_sync_invoice,
    enqueue_sync_product,
    enqueue_void_invoice,
)
from app.services.accounting.revenue_account_mapping import (
    list_provider_revenue_accounts,
    resolve_revenue_account,
)
from app.services.secrets import decrypt_string, encrypt_string


def _configure_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quickbooks_client_id", "qb-client-id")
    monkeypatch.setattr(settings, "quickbooks_client_secret", "qb-client-secret")
    monkeypatch.setattr(settings, "quickbooks_environment", "sandbox")
    monkeypatch.setattr(
        settings,
        "app_encryption_key",
        Fernet.generate_key().decode("ascii"),
    )


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        request=httpx.Request("GET", "https://quickbooks.test"),
    )


def _seed_connection(
    db_session,
    *,
    tenant_id: int = 1,
    access_token: str = "qb-access-token",
    refresh_token: str = "qb-refresh-token",
    expires_at: datetime | None = None,
) -> AccountingConnection:
    connection = AccountingConnection(
        tenant_id=tenant_id,
        provider="quickbooks",
        status="connected",
        realm_id=f"realm-{tenant_id}",
        encrypted_access_token=encrypt_string(access_token),
        encrypted_refresh_token=encrypt_string(refresh_token),
        access_token_expires_at=expires_at or (datetime.utcnow() + timedelta(days=30)),
        refresh_token_expires_at=datetime.utcnow() + timedelta(days=180),
        scopes="com.intuit.quickbooks.accounting",
        connected_at=datetime.utcnow(),
    )
    db_session.add(connection)
    db_session.commit()
    return connection


def _job(db_session, *, job_type: str) -> AccountingSyncJob:
    return db_session.execute(
        select(AccountingSyncJob)
        .where(AccountingSyncJob.job_type == job_type)
        .order_by(AccountingSyncJob.id.desc())
        .limit(1)
    ).scalar_one()


def _event_payloads(db_session, *, event_type: str) -> list[str]:
    events = db_session.execute(
        select(AccountingSyncEvent)
        .where(AccountingSyncEvent.event_type == event_type)
        .order_by(AccountingSyncEvent.id.asc())
    ).scalars()
    return [json.dumps(event.detail_json or {}, sort_keys=True) for event in events]


def _seed_tax_map(
    db_session,
    *,
    tax_rate: TaxRate,
    external_id: str | None = None,
    external_code: str | None = None,
    tenant_id: int = 1,
    provider: str = "quickbooks",
    is_active: bool = True,
) -> AccountingTaxMap:
    tax_map = AccountingTaxMap(
        tenant_id=tenant_id,
        provider=provider,
        tax_rate_id=tax_rate.id,
        external_id=external_id,
        external_code=external_code,
        is_active=is_active,
    )
    db_session.add(tax_map)
    db_session.commit()
    return tax_map


def test_customer_job_runs_and_writes_customer_map(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-JOB-1", name="QuickBooks Customer One")
    db_session.add(customer)
    db_session.commit()
    _seed_connection(db_session)

    enqueue_sync_customer(db_session, tenant_id=1, customer_id=customer.id)
    db_session.commit()

    calls = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        calls.append((method, url, params, json, headers))
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/customer"):
            assert headers["Authorization"] == "Bearer qb-access-token"
            return _response(
                200,
                {
                    "Customer": {
                        "Id": "QB-CUST-1",
                        "SyncToken": "0",
                        "DisplayName": "C-QB-JOB-1 - QuickBooks Customer One",
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    assert result.failed == 0
    mapping = db_session.execute(
        select(AccountingCustomerMap).where(AccountingCustomerMap.customer_id == customer.id)
    ).scalar_one()
    assert mapping.external_id == "QB-CUST-1"
    assert mapping.sync_status == "synced"
    assert _job(db_session, job_type="sync_customer").status == "succeeded"
    assert any("QB-CUST-1" in payload for payload in _event_payloads(db_session, event_type="customer_synced"))
    assert all("qb-access-token" not in payload for payload in _event_payloads(db_session, event_type="job_succeeded"))
    assert len(calls) == 2


def test_disconnected_tenant_has_no_outbound_accounting_work(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-DISCONNECTED", name="Disconnected Customer")
    db_session.add(customer)
    db_session.commit()

    assert enqueue_sync_customer(db_session, tenant_id=1, customer_id=customer.id) is None
    db_session.commit()

    def _unexpected_request(*args, **kwargs):
        raise AssertionError("QuickBooks should not be called when no tenant connection is active.")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", _unexpected_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=5)

    assert result.processed == 0
    assert result.succeeded == 0
    assert result.failed == 0
    assert db_session.execute(select(AccountingSyncJob)).scalars().all() == []


def test_customer_job_updates_existing_remote_customer(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-REMOTE-1", name="Existing Remote Customer")
    db_session.add(customer)
    db_session.flush()
    db_session.add(
        AccountingCustomerMap(
            tenant_id=1,
            provider="quickbooks",
            customer_id=customer.id,
            external_id="QB-CUST-REMOTE-1",
            sync_status="failed",
            payload_hash="stale",
        )
    )
    db_session.commit()
    _seed_connection(db_session)

    enqueue_sync_customer(db_session, tenant_id=1, customer_id=customer.id)
    db_session.commit()

    calls: list[tuple[str, str, str | None]] = []

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/customer/QB-CUST-REMOTE-1"):
            calls.append((method, url, None))
            return _response(
                200,
                {
                    "Customer": {
                        "Id": "QB-CUST-REMOTE-1",
                        "SyncToken": "4",
                        "DisplayName": "C-QB-REMOTE-1 - Existing Remote Customer",
                    }
                },
            )
        if url.endswith("/customer") and str((params or {}).get("operation")) == "update":
            calls.append((method, url, "update"))
            assert json["Id"] == "QB-CUST-REMOTE-1"
            assert json["SyncToken"] == "4"
            assert json["sparse"] is True
            return _response(
                200,
                {
                    "Customer": {
                        "Id": "QB-CUST-REMOTE-1",
                        "SyncToken": "5",
                        "DisplayName": "C-QB-REMOTE-1 - Existing Remote Customer",
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/customer/QB-CUST-REMOTE-1")
    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/customer")
    assert calls[1][2] == "update"
    mapping = db_session.execute(
        select(AccountingCustomerMap).where(AccountingCustomerMap.customer_id == customer.id)
    ).scalar_one()
    assert mapping.external_id == "QB-CUST-REMOTE-1"
    assert mapping.sync_status == "synced"
    assert _job(db_session, job_type="sync_customer").status == "succeeded"


def test_quickbooks_revenue_account_discovery_filters_income_accounts(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    _seed_connection(db_session)

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM Account" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "Account": [
                            {
                                "Id": "79",
                                "AcctNum": "4100",
                                "Name": "Sales Income",
                                "AccountType": "Income",
                                "AccountSubType": "SalesOfProductIncome",
                                "Classification": "Revenue",
                                "Active": True,
                            },
                            {
                                "Id": "80",
                                "AcctNum": "5000",
                                "Name": "Expense Account",
                                "AccountType": "Expense",
                                "Classification": "Expense",
                                "Active": True,
                            },
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    accounts = list_provider_revenue_accounts(db_session, tenant_id=1, provider="quickbooks")

    assert len(accounts) == 1
    assert accounts[0].remote_account_id == "79"
    assert accounts[0].remote_account_code == "4100"
    assert accounts[0].remote_account_name == "Sales Income"
    assert accounts[0].remote_account_type == "Income"
    assert accounts[0].remote_account_detail_type == "SalesOfProductIncome"
    assert accounts[0].is_active is True
    assert accounts[0].is_usable is True


def test_quickbooks_tax_code_discovery_accepts_description_only_records(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    _seed_connection(db_session)

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Description": "20.0% S",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    tax_codes = tax_mapping_module.list_provider_tax_codes(
        db_session,
        tenant_id=1,
        provider="quickbooks",
    )

    assert len(tax_codes) == 1
    assert tax_codes[0].remote_tax_code_id == "3"
    assert tax_codes[0].display_code == "20.0% S"
    assert tax_codes[0].display_name == "20.0% S"


def test_product_job_runs_and_writes_product_map(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    tax_rate = TaxRate(code="QB VAT", description="QB VAT", rate_percent=Decimal("20.000"), is_active=True)
    product = Product(
        code="QB-PROD-1",
        description="QuickBooks Product One",
        nominal_code="4000",
        unit_price=Decimal("25.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([tax_rate, product])
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_product(db_session, tenant_id=1, product_id=product.id)
    db_session.commit()

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM Account" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "Account": [
                            {
                                "Id": "79",
                                "AcctNum": "4000",
                                "AccountType": "Income",
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query") and "FROM Item" in str((params or {}).get("query")):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/item"):
            assert json["IncomeAccountRef"]["value"] == "79"
            assert json["Taxable"] is True
            return _response(
                200,
                {
                    "Item": {
                        "Id": "QB-ITEM-1",
                        "SyncToken": "0",
                        "Name": "QB-PROD-1",
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    mapping = db_session.execute(
        select(AccountingProductMap).where(AccountingProductMap.product_id == product.id)
    ).scalar_one()
    assert mapping.external_id == "QB-ITEM-1"
    assert mapping.sync_status == "synced"
    assert _job(db_session, job_type="sync_product").status == "succeeded"
    product_synced_payloads = _event_payloads(db_session, event_type="product_synced")
    assert any("nominal_code_fallback" in payload for payload in product_synced_payloads)


def test_product_job_updates_existing_remote_product(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    tax_rate = TaxRate(
        code="QB VAT PROD REMOTE",
        description="QB VAT Product Remote",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-PROD-REMOTE-1",
        description="Existing Remote Product",
        nominal_code="4000",
        unit_price=Decimal("25.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([tax_rate, product])
    db_session.flush()
    db_session.add(
        AccountingProductMap(
            tenant_id=1,
            provider="quickbooks",
            product_id=product.id,
            external_id="QB-ITEM-REMOTE-1",
            sync_status="failed",
            payload_hash="stale",
        )
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_product(db_session, tenant_id=1, product_id=product.id)
    db_session.commit()

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM Account" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "Account": [
                            {
                                "Id": "79",
                                "AcctNum": "4000",
                                "AccountType": "Income",
                                "AccountSubType": "SalesOfProductIncome",
                                "Classification": "Revenue",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/item/QB-ITEM-REMOTE-1"):
            return _response(
                200,
                {
                    "Item": {
                        "Id": "QB-ITEM-REMOTE-1",
                        "SyncToken": "6",
                        "Name": "QB-PROD-REMOTE-1",
                    }
                },
            )
        if url.endswith("/item") and str((params or {}).get("operation")) == "update":
            assert json["Id"] == "QB-ITEM-REMOTE-1"
            assert json["SyncToken"] == "6"
            assert json["sparse"] is True
            assert json["IncomeAccountRef"]["value"] == "79"
            return _response(
                200,
                {
                    "Item": {
                        "Id": "QB-ITEM-REMOTE-1",
                        "SyncToken": "7",
                        "Name": "QB-PROD-REMOTE-1",
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    mapping = db_session.execute(
        select(AccountingProductMap).where(AccountingProductMap.product_id == product.id)
    ).scalar_one()
    assert mapping.external_id == "QB-ITEM-REMOTE-1"
    assert mapping.sync_status == "synced"
    assert _job(db_session, job_type="sync_product").status == "succeeded"


def test_product_job_fails_clearly_when_nominal_code_is_missing(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    tax_rate = TaxRate(
        code="QB VAT FAIL",
        description="QB VAT FAIL",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-PROD-NOMINAL-FAIL",
        description="QuickBooks Product Missing Nominal",
        unit_price=Decimal("25.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([tax_rate, product])
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_product(db_session, tenant_id=1, product_id=product.id)
    db_session.commit()

    def _unexpected_request(*args, **kwargs):
        raise AssertionError("QuickBooks should not be called when nominal code is missing.")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", _unexpected_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="sync_product")
    assert job.status == "failed"
    assert "no default revenue account is selected" in (job.error_text or "").lower()
    assert "no nominal code fallback" in (job.error_text or "").lower()


def test_invoice_job_runs_and_writes_invoice_sync_row(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-1", name="Invoice Sync Customer")
    tax_rate = TaxRate(
        code="QB VAT INV",
        description="QB VAT Invoice",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-PROD-1",
        description="Invoice Sync Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="QB-INV-T-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("12.00"),
        dont_invoice=False,
        paid=False,
    )
    invoice = Invoice(
        invoice_no="INV-QB-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([ticket, invoice])
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket.id,
            description="Invoice Sync Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.add_all(
        [
            AccountingCustomerMap(
                tenant_id=1,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-INV",
                sync_status="synced",
                payload_hash="ready",
            ),
            AccountingProductMap(
                tenant_id=1,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-ITEM-INV",
                sync_status="synced",
                payload_hash="ready",
            ),
        ]
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-INV"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-INV"},
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            line_tax_ref = json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"]
            if line_tax_ref == "TAX" or "TxnTaxDetail" in json or json.get("GlobalTaxCalculation") != "TaxExcluded":
                return _response(
                    400,
                    {
                        "Fault": {
                            "type": "ValidationFault",
                            "Error": [
                                {
                                    "code": "6000",
                                    "Message": "Business Validation Error",
                                    "Detail": "Make sure all your transactions have a VAT rate before you save.",
                                }
                            ],
                        }
                    },
            )
            assert json["DocNumber"] == "INV-QB-1"
            assert json["Line"][0]["Amount"] == 10.0
            assert line_tax_ref == "3"
            assert "TxnTaxDetail" not in json
            assert json["GlobalTaxCalculation"] == "TaxExcluded"
            return _response(
                200,
                {
                    "Invoice": {
                        "Id": "QB-INV-1",
                        "DocNumber": "INV-QB-1",
                        "SyncToken": "0",
                        "TotalAmt": 12.0,
                        "TxnTaxDetail": {"TotalTax": 2.0},
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.external_id == "QB-INV-1"
    assert sync_row.external_doc_number == "INV-QB-1"
    assert sync_row.sync_status == "invoice_synced"
    assert sync_row.provider_response_json["line_amount_basis"] == "net_exclusive"
    assert sync_row.provider_response_json["global_tax_calculation"] == "TaxExcluded"
    assert sync_row.provider_response_json["tax_payload_summary"]["line_level_tax_fields_sent"] is True
    assert sync_row.provider_response_json["tax_payload_summary"]["invoice_level_tax_fields_sent"] is False
    assert (
        sync_row.provider_response_json["tax_payload_summary"]["mapped_tax_refs"][0]["stored_provider_ref"]
        == "3"
    )
    assert sync_row.provider_response_json["tax_payload_summary"]["mapped_tax_refs"][0]["display_code"] == "20.0% S"
    assert sync_row.provider_response_json["tax_payload_summary"]["mapped_tax_refs"][0]["actual_tax_code_ref_sent"] == "3"
    assert sync_row.provider_response_json["local_totals"]["gross_total"] == 12.0
    assert _job(db_session, job_type="sync_invoice").status == "succeeded"


def test_zero_vat_invoice_job_syncs_successfully(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-ZERO", name="Zero VAT Invoice Customer")
    tax_rate = TaxRate(
        code="QB VAT ZERO",
        description="QuickBooks VAT Zero",
        rate_percent=Decimal("0.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-ZERO-PROD",
        description="Zero VAT Invoice Product",
        nominal_code="4000",
        unit_price=Decimal("10.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="QB-INV-ZERO-T-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("10.00"),
        dont_invoice=False,
        paid=False,
    )
    invoice = Invoice(
        invoice_no="INV-QB-ZERO-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("10.00"),
    )
    db_session.add_all([ticket, invoice])
    db_session.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Zero VAT Invoice Line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("10.00"),
        net=Decimal("10.00"),
        vat=Decimal("0.00"),
        gross=Decimal("10.00"),
        product_snapshot_json={
            "product_id": product.id,
            "product_code": product.code,
            "tax_rate_id": tax_rate.id,
            "tax_rate_code": tax_rate.code,
            "tax_rate_percent": "0.000",
            "nominal_code": "4000",
        },
    )
    db_session.add(line)
    db_session.add_all(
        [
            AccountingCustomerMap(
                tenant_id=1,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-ZERO-1",
                sync_status="synced",
                payload_hash="ready",
            ),
            AccountingProductMap(
                tenant_id=1,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-ITEM-ZERO-1",
                sync_status="synced",
                payload_hash="ready",
            ),
        ]
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="2",
        external_code="Exempt From VAT",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-ZERO-1"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-ZERO-1"},
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "2",
                                "Name": "Exempt From VAT",
                                "Description": "Exempt From VAT",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            assert json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "2"
            assert "TxnTaxDetail" not in json
            assert json["GlobalTaxCalculation"] == "TaxExcluded"
            return _response(
                200,
                {
                    "Invoice": {
                        "Id": "QB-INV-ZERO-1",
                        "DocNumber": "INV-QB-ZERO-1",
                        "SyncToken": "0",
                        "TotalAmt": 10.0,
                        "TxnTaxDetail": {"TotalTax": 0.0},
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.external_id == "QB-INV-ZERO-1"
    assert sync_row.sync_status == "invoice_synced"
    assert sync_row.provider_response_json["tax_payload_summary"]["mapped_tax_refs"] == [
        {
            "invoice_line_id": line.id,
            "tax_rate_id": tax_rate.id,
            "local_tax_label": "QB VAT ZERO (QuickBooks VAT Zero)",
            "stored_provider_ref": "2",
            "display_code": "Exempt From VAT",
            "actual_tax_code_ref_sent": "2",
        }
    ]


def test_invoice_job_supports_mixed_explicit_uk_tax_refs(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-MIX-1", name="Invoice Mixed Tax Customer")
    taxable_rate = TaxRate(
        code="QB VAT 20",
        description="QuickBooks VAT 20",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    zero_rate = TaxRate(
        code="QB VAT 0",
        description="QuickBooks VAT 0",
        rate_percent=Decimal("0.000"),
        is_active=True,
    )
    product_a = Product(
        code="QB-INV-MIX-A",
        description="Invoice Mixed Product A",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=taxable_rate,
    )
    product_b = Product(
        code="QB-INV-MIX-B",
        description="Invoice Mixed Product B",
        nominal_code="4000",
        unit_price=Decimal("5.00"),
        tax_rate=zero_rate,
    )
    invoice = Invoice(
        invoice_no="INV-QB-MIX-1",
        customer_id=1,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("15.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("17.00"),
    )
    db_session.add_all([customer, taxable_rate, zero_rate, product_a, product_b])
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.flush()
    line_one = InvoiceLine(
        invoice_id=invoice.id,
        description="Invoice Mixed Line 1",
        quantity=Decimal("1.000"),
        unit_price=Decimal("10.00"),
        net=Decimal("10.00"),
        vat=Decimal("2.00"),
        gross=Decimal("12.00"),
        product_snapshot_json={
            "product_id": product_a.id,
            "product_code": product_a.code,
            "tax_rate_id": taxable_rate.id,
            "tax_rate_code": taxable_rate.code,
            "tax_rate_percent": "20.000",
            "nominal_code": "4000",
        },
    )
    line_two = InvoiceLine(
        invoice_id=invoice.id,
        description="Invoice Mixed Line 2",
        quantity=Decimal("1.000"),
        unit_price=Decimal("5.00"),
        net=Decimal("5.00"),
        vat=Decimal("0.00"),
        gross=Decimal("5.00"),
        product_snapshot_json={
            "product_id": product_b.id,
            "product_code": product_b.code,
            "tax_rate_id": zero_rate.id,
            "tax_rate_code": zero_rate.code,
            "tax_rate_percent": "0.000",
            "nominal_code": "4000",
        },
    )
    db_session.add_all([line_one, line_two])
    db_session.flush()
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=taxable_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_tax_map(
        db_session,
        tax_rate=zero_rate,
        external_id="2",
        external_code="Exempt From VAT",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-INV-MIX"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {
            "external_id": "QB-ITEM-MIX-A"
            if int(kwargs["product_id"]) == int(product_a.id)
            else "QB-ITEM-MIX-B"
        },
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            },
                            {
                                "Id": "2",
                                "Name": "Exempt From VAT",
                                "Description": "Exempt From VAT",
                                "Active": True,
                            },
                        ]
                    }
                },
            )
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            assert json["GlobalTaxCalculation"] == "TaxExcluded"
            assert "TxnTaxDetail" not in json
            assert len(json["Line"]) == 2
            assert json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "3"
            assert json["Line"][1]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "2"
            return _response(
                200,
                {
                    "Invoice": {
                        "Id": "QB-INV-MIX-1",
                        "DocNumber": "INV-QB-MIX-1",
                        "SyncToken": "0",
                        "TotalAmt": 17.0,
                        "TxnTaxDetail": {"TotalTax": 2.0},
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.sync_status == "invoice_synced"
    assert sync_row.provider_response_json["tax_payload_summary"]["mapped_tax_refs"] == [
        {
            "invoice_line_id": line_one.id,
            "tax_rate_id": taxable_rate.id,
            "local_tax_label": "QB VAT 20 (QuickBooks VAT 20)",
            "stored_provider_ref": "3",
            "display_code": "20.0% S",
            "actual_tax_code_ref_sent": "3",
        },
        {
            "invoice_line_id": line_two.id,
            "tax_rate_id": zero_rate.id,
            "local_tax_label": "QB VAT 0 (QuickBooks VAT 0)",
            "stored_provider_ref": "2",
            "display_code": "Exempt From VAT",
            "actual_tax_code_ref_sent": "2",
        },
    ]


def test_invoice_job_logs_tax_payload_summary_when_quickbooks_rejects_vat_payload(
    db_session,
    monkeypatch,
    caplog,
):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-VAT-FAIL", name="Invoice VAT Failure Customer")
    tax_rate = TaxRate(
        code="QB VAT FAIL LOG",
        description="QuickBooks VAT Fail Log",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-VAT-FAIL",
        description="Invoice VAT Failure Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    invoice = Invoice(
        invoice_no="INV-QB-VAT-FAIL",
        customer_id=1,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            description="Invoice VAT Failure Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-VAT-FAIL"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-VAT-FAIL"},
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            return _response(
                400,
                {
                    "Fault": {
                        "type": "ValidationFault",
                        "Error": [
                            {
                                "code": "6000",
                                "Message": "Business Validation Error",
                                "Detail": "Make sure all your transactions have a VAT rate before you save.",
                            }
                        ],
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    with caplog.at_level(logging.WARNING):
        result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="sync_invoice")
    assert job.status == "failed"
    assert "vat rate before you save" in (job.error_text or "").lower()
    assert f"QuickBooks invoice sync failed for invoice {invoice.id}" in caplog.text
    failed_payloads = _event_payloads(db_session, event_type="job_failed")
    assert any('"tax_payload_summary"' in payload for payload in failed_payloads)
    assert any('"stored_provider_ref": "3"' in payload for payload in failed_payloads)
    assert any('"actual_tax_code_ref_sent": "3"' in payload for payload in failed_payloads)


def test_invoice_job_fails_clearly_when_tax_mapping_uses_display_code_as_provider_ref(
    db_session,
    monkeypatch,
):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-LEGACY-TAX", name="Invoice Legacy Tax Customer")
    tax_rate = TaxRate(
        code="QB VAT LEGACY",
        description="QuickBooks VAT Legacy",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-LEGACY-PROD",
        description="Invoice Legacy Tax Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    invoice = Invoice(
        invoice_no="INV-QB-LEGACY-TAX",
        customer_id=1,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            description="Invoice Legacy Tax Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="20.0% S",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-LEGACY-TAX"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-LEGACY-TAX"},
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/invoice"):
            raise AssertionError("Invoice should not be posted when the stored provider ref is only a display code.")
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="sync_invoice")
    assert job.status == "failed"
    assert "display code/label" in (job.error_text or "").lower()
    assert "re-save this mapping" in (job.error_text or "").lower()


def test_invoice_job_prefers_default_revenue_account_mapping_over_nominal_fallback(
    db_session,
    monkeypatch,
):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-MAP-1", name="Invoice Mapping Customer")
    tax_rate = TaxRate(
        code="QB VAT MAP",
        description="QB VAT Map",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-PROD-MAP-1",
        description="Invoice Mapping Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="QB-INV-T-MAP-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("12.00"),
        dont_invoice=False,
        paid=False,
    )
    invoice = Invoice(
        invoice_no="INV-QB-MAP-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([ticket, invoice])
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket.id,
            description="Invoice Mapping Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.add(
        AccountingRevenueAccountMap(
            tenant_id=1,
            provider="quickbooks",
            local_scope_type="global_default",
            remote_account_id="79",
            remote_account_code="4100",
            remote_account_name="Mapped Sales Income",
            remote_account_type="Income",
            is_active=True,
        )
    )
    db_session.add_all(
        [
            AccountingCustomerMap(
                tenant_id=1,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-INV-MAP",
                sync_status="synced",
                payload_hash="ready",
            ),
            AccountingProductMap(
                tenant_id=1,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-ITEM-INV-MAP",
                sync_status="synced",
                payload_hash="stale",
            ),
        ]
    )
    db_session.commit()
    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-INV-MAP"},
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM Account" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "Account": [
                            {
                                "Id": "79",
                                "AcctNum": "4100",
                                "Name": "Mapped Sales Income",
                                "AccountType": "Income",
                                "AccountSubType": "SalesOfProductIncome",
                                "Classification": "Revenue",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query") and "FROM Item" in str((params or {}).get("query")):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/item/QB-ITEM-INV-MAP"):
            return _response(
                200,
                {
                    "Item": {
                        "Id": "QB-ITEM-INV-MAP",
                        "SyncToken": "1",
                        "Name": "QB-INV-PROD-MAP-1",
                    }
                },
            )
        if url.endswith("/item") and str((params or {}).get("operation")) == "update":
            assert json["IncomeAccountRef"]["value"] == "79"
            return _response(
                200,
                {
                    "Item": {
                        "Id": "QB-ITEM-INV-MAP",
                        "SyncToken": "2",
                        "Name": "QB-INV-PROD-MAP-1",
                    }
                },
            )
        if url.endswith("/query") and "FROM Invoice" in str((params or {}).get("query")):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            assert json["DocNumber"] == "INV-QB-MAP-1"
            assert json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "3"
            return _response(
                200,
                {
                    "Invoice": {
                        "Id": "QB-INV-MAP-1",
                        "DocNumber": "INV-QB-MAP-1",
                        "SyncToken": "0",
                        "TotalAmt": 12.0,
                        "TxnTaxDetail": {"TotalTax": 2.0},
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.sync_status == "invoice_synced"
    product_synced_payloads = _event_payloads(db_session, event_type="product_synced")
    assert any("global_default_mapping" in payload for payload in product_synced_payloads)


def test_default_mapping_overrides_nominal_code(db_session, caplog):
    class FakeQuickBooksClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def resolve_income_account_ref_by_id(self, *, remote_account_id: str) -> str:
            self.calls.append(("ref_by_id", remote_account_id))
            return remote_account_id

        def resolve_income_account_by_id(self, *, remote_account_id: str):
            self.calls.append(("by_id", remote_account_id))
            return quickbooks_client_module.QuickBooksRevenueAccount(
                remote_account_id=remote_account_id,
                remote_account_code="4000",
                remote_account_name="Mapped Sales Income",
                remote_account_type="Income",
                remote_account_detail_type="SalesOfProductIncome",
                is_active=True,
                is_usable=True,
            )

        def resolve_income_account_ref_by_nominal_code(self, *, nominal_code: str) -> str:
            raise AssertionError("Nominal fallback should not be used when default mapping exists.")

        def resolve_income_account_by_nominal_code(self, *, nominal_code: str):
            raise AssertionError("Nominal fallback should not be used when default mapping exists.")

    db_session.add(
        AccountingRevenueAccountMap(
            tenant_id=1,
            provider="quickbooks",
            local_scope_type="global_default",
            remote_account_id="79",
            remote_account_code="4000",
            remote_account_name="Mapped Sales Income",
            remote_account_type="Income",
            is_active=True,
        )
    )
    db_session.commit()

    client = FakeQuickBooksClient()

    with caplog.at_level(logging.INFO):
        resolved = resolve_revenue_account(
            db_session,
            tenant_id=1,
            provider="quickbooks",
            product_label="Product QB-MAP",
            nominal_code="4100",
            client=client,
        )

    assert resolved.remote_account_id == "79"
    assert resolved.remote_account_code == "4000"
    assert resolved.remote_account_name == "Mapped Sales Income"
    assert resolved.resolution_source == "global_default_mapping"
    assert client.calls == [("ref_by_id", "79"), ("by_id", "79")]
    assert "Using default QB revenue account mapping: Mapped Sales Income" in caplog.text


def test_mapping_does_not_fallback(db_session):
    class FakeQuickBooksClient:
        def resolve_income_account_ref_by_id(self, *, remote_account_id: str) -> str:
            raise quickbooks_client_module.QuickBooksApiError(
                "QuickBooks revenue account 79 was not found among active income/revenue accounts."
            )

        def resolve_income_account_by_id(self, *, remote_account_id: str):
            raise AssertionError("Resolver should fail before reloading an invalid mapped account.")

        def resolve_income_account_ref_by_nominal_code(self, *, nominal_code: str) -> str:
            raise AssertionError("Nominal fallback should not run when a default mapping exists.")

        def resolve_income_account_by_nominal_code(self, *, nominal_code: str):
            raise AssertionError("Nominal fallback should not run when a default mapping exists.")

    db_session.add(
        AccountingRevenueAccountMap(
            tenant_id=1,
            provider="quickbooks",
            local_scope_type="global_default",
            remote_account_id="79",
            remote_account_code="4000",
            remote_account_name="Broken Sales Income",
            remote_account_type="Income",
            is_active=True,
        )
    )
    db_session.commit()

    try:
        resolve_revenue_account(
            db_session,
            tenant_id=1,
            provider="quickbooks",
            product_label="Product QB-BROKEN",
            nominal_code="4100",
            client=FakeQuickBooksClient(),
        )
        raise AssertionError("Expected the invalid mapped account to fail without fallback.")
    except quickbooks_client_module.QuickBooksApiError as exc:
        assert str(exc) == "Configured default QuickBooks revenue account is invalid or not usable"


def test_nominal_used_when_no_mapping(db_session, caplog):
    class FakeQuickBooksClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def resolve_income_account_ref_by_id(self, *, remote_account_id: str) -> str:
            raise AssertionError("Default mapping lookup should not run when no mapping exists.")

        def resolve_income_account_by_id(self, *, remote_account_id: str):
            raise AssertionError("Default mapping lookup should not run when no mapping exists.")

        def resolve_income_account_ref_by_nominal_code(self, *, nominal_code: str) -> str:
            self.calls.append(("ref_by_nominal_code", nominal_code))
            return "88"

        def resolve_income_account_by_nominal_code(self, *, nominal_code: str):
            self.calls.append(("by_nominal_code", nominal_code))
            return quickbooks_client_module.QuickBooksRevenueAccount(
                remote_account_id="88",
                remote_account_code=nominal_code,
                remote_account_name="Nominal Sales Income",
                remote_account_type="Income",
                remote_account_detail_type="SalesOfProductIncome",
                is_active=True,
                is_usable=True,
            )

    client = FakeQuickBooksClient()

    with caplog.at_level(logging.INFO):
        resolved = resolve_revenue_account(
            db_session,
            tenant_id=1,
            provider="quickbooks",
            product_label="Product QB-NOMINAL",
            nominal_code="4100",
            client=client,
        )

    assert resolved.remote_account_id == "88"
    assert resolved.remote_account_code == "4100"
    assert resolved.remote_account_name == "Nominal Sales Income"
    assert resolved.resolution_source == "nominal_code_fallback"
    assert client.calls == [
        ("ref_by_nominal_code", "4100"),
        ("by_nominal_code", "4100"),
    ]
    assert "Using nominal code fallback: 4100" in caplog.text


def test_invoice_job_fails_clearly_when_tax_mapping_is_missing(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-MISS-TAX", name="Invoice Missing Tax Map")
    tax_rate = TaxRate(
        code="QB VAT MISS",
        description="QB VAT Missing",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-PROD-MISS",
        description="Invoice Missing Tax Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="QB-INV-T-MISS",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("12.00"),
        dont_invoice=False,
        paid=False,
    )
    invoice = Invoice(
        invoice_no="INV-QB-MISS-TAX",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([ticket, invoice])
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket.id,
            description="Invoice Missing Tax Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.add_all(
        [
            AccountingCustomerMap(
                tenant_id=1,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-INV-MISS",
                sync_status="synced",
                payload_hash="ready",
            ),
            AccountingProductMap(
                tenant_id=1,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-ITEM-INV-MISS",
                sync_status="synced",
                payload_hash="ready",
            ),
        ]
    )
    db_session.commit()
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-INV-MISS"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-INV-MISS"},
    )

    def _unexpected_request(*args, **kwargs):
        raise AssertionError("QuickBooks should not be called when tax mapping is missing.")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", _unexpected_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="sync_invoice")
    assert job.status == "failed"
    assert "no quickbooks tax mapping" in (job.error_text or "").lower()
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.sync_status == "failed"
    assert "no QuickBooks tax mapping".lower() in str(sync_row.last_error or "").lower()


def test_failed_invoice_job_retries_successfully_after_fixing_tax_mapping(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-INV-RETRY", name="Invoice Retry Customer")
    tax_rate = TaxRate(
        code="QB VAT RETRY",
        description="QuickBooks VAT Retry",
        rate_percent=Decimal("20.000"),
        is_active=True,
    )
    product = Product(
        code="QB-INV-RETRY-PROD",
        description="Invoice Retry Product",
        nominal_code="4000",
        unit_price=Decimal("12.00"),
        tax_rate=tax_rate,
    )
    db_session.add_all([customer, tax_rate, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="QB-INV-RETRY-T-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("12.00"),
        dont_invoice=False,
        paid=False,
    )
    invoice = Invoice(
        invoice_no="INV-QB-RETRY-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 12),
        due_date=date(2026, 3, 12),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add_all([ticket, invoice])
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            ticket_id=ticket.id,
            description="Invoice Retry Line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("10.00"),
            net=Decimal("10.00"),
            vat=Decimal("2.00"),
            gross=Decimal("12.00"),
            product_snapshot_json={
                "product_id": product.id,
                "product_code": product.code,
                "tax_rate_id": tax_rate.id,
                "tax_rate_code": tax_rate.code,
                "tax_rate_percent": "20.000",
                "nominal_code": "4000",
            },
        )
    )
    db_session.add_all(
        [
            AccountingCustomerMap(
                tenant_id=1,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-RETRY-1",
                sync_status="synced",
                payload_hash="ready",
            ),
            AccountingProductMap(
                tenant_id=1,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-ITEM-RETRY-1",
                sync_status="synced",
                payload_hash="ready",
            ),
        ]
    )
    db_session.commit()
    _seed_connection(db_session)

    enqueue_sync_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-RETRY-1"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_product_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-ITEM-RETRY-1"},
    )

    def _unexpected_request(*args, **kwargs):
        raise AssertionError("QuickBooks should not be called before the missing tax mapping is fixed.")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", _unexpected_request)

    first_result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert first_result.processed == 1
    assert first_result.failed == 1
    failed_job = _job(db_session, job_type="sync_invoice")
    failed_job_id = int(failed_job.id)
    assert failed_job.status == "failed"
    assert "no quickbooks tax mapping" in (failed_job.error_text or "").lower()

    _seed_tax_map(
        db_session,
        tax_rate=tax_rate,
        external_id="3",
        external_code="20.0% S",
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query") and "FROM TaxCode" in str((params or {}).get("query")):
            return _response(
                200,
                {
                    "QueryResponse": {
                        "TaxCode": [
                            {
                                "Id": "3",
                                "Name": "20.0% S",
                                "Description": "20.0% Standard Sales",
                                "Active": True,
                            }
                        ]
                    }
                },
            )
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            assert json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "3"
            return _response(
                200,
                {
                    "Invoice": {
                        "Id": "QB-INV-RETRY-1",
                        "DocNumber": "INV-QB-RETRY-1",
                        "SyncToken": "0",
                        "TotalAmt": 12.0,
                        "TxnTaxDetail": {"TotalTax": 2.0},
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    retry_result = process_pending_accounting_jobs(
        db_session,
        tenant_id=1,
        limit=1,
        retry_failed=True,
    )

    assert retry_result.processed == 1
    assert retry_result.succeeded == 1
    retried_job = _job(db_session, job_type="sync_invoice")
    assert retried_job.id == failed_job_id
    assert retried_job.status == "succeeded"
    assert retried_job.attempts == 2
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.external_id == "QB-INV-RETRY-1"
    assert sync_row.sync_status == "invoice_synced"


def test_paid_job_runs_and_updates_sync_state(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-PAID-1", name="Paid Sync Customer")
    invoice = Invoice(
        invoice_no="INV-QB-PAID-1",
        customer_id=1,
        invoice_date=date(2026, 1, 10),
        due_date=date(2026, 1, 20),
        status="PAID",
        paid_at=datetime(2026, 1, 12, 8, 30, 0),
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(customer)
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        AccountingInvoiceSync(
            tenant_id=1,
            provider="quickbooks",
            invoice_id=invoice.id,
            external_id="QB-INV-PAID-1",
            external_doc_number="INV-QB-PAID-1",
            sync_status="invoice_synced",
        )
    )
    db_session.commit()
    _seed_connection(db_session)

    enqueue_mark_invoice_paid(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-PAID-1"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_invoice_to_quickbooks",
        lambda *args, **kwargs: {
            "external_id": "QB-INV-PAID-1",
            "external_doc_number": "INV-QB-PAID-1",
        },
    )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/invoice/QB-INV-PAID-1"):
            return _response(
                200,
                {"Invoice": {"Id": "QB-INV-PAID-1", "Balance": 12, "SyncToken": "1"}},
            )
        if url.endswith("/payment"):
            return _response(
                200,
                {"Payment": {"Id": "QB-PAY-1", "SyncToken": "0"}},
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.sync_status == "payment_synced"
    assert sync_row.provider_response_json["payment"]["id"] == "QB-PAY-1"


def test_paid_job_fails_when_remote_invoice_is_missing(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-PAID-MISSING", name="Paid Missing Invoice Customer")
    invoice = Invoice(
        invoice_no="INV-QB-PAID-MISSING",
        customer_id=1,
        invoice_date=date(2026, 1, 10),
        due_date=date(2026, 1, 20),
        status="PAID",
        paid_at=datetime(2026, 1, 12, 8, 30, 0),
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(customer)
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.commit()
    _seed_connection(db_session)

    enqueue_mark_invoice_paid(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    monkeypatch.setattr(
        invoice_sync_module,
        "sync_customer_to_quickbooks",
        lambda *args, **kwargs: {"external_id": "QB-CUST-PAID-MISSING"},
    )
    monkeypatch.setattr(
        invoice_sync_module,
        "sync_invoice_to_quickbooks",
        lambda *args, **kwargs: {
            "external_id": "QB-INV-MISSING",
            "external_doc_number": "INV-QB-PAID-MISSING",
        },
    )

    payment_called = {"value": False}

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/invoice/QB-INV-MISSING"):
            return _response(
                400,
                {
                    "Fault": {
                        "type": "ValidationFault",
                        "Error": [
                            {
                                "code": "610",
                                "Message": "Object Not Found",
                                "Detail": "Object Not Found : Something you're trying to use has been made inactive.",
                            }
                        ],
                    }
                },
            )
        if url.endswith("/payment"):
            payment_called["value"] = True
            raise AssertionError("Payment should not be created when the remote invoice does not exist.")
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    assert payment_called["value"] is False
    job = _job(db_session, job_type="mark_invoice_paid")
    assert job.status == "failed"
    sync_row = db_session.execute(
        select(AccountingInvoiceSync).where(AccountingInvoiceSync.invoice_id == invoice.id)
    ).scalar_one()
    assert sync_row.sync_status == "failed"


def test_failed_provider_response_marks_job_failed_cleanly(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-FAIL-1", name="QuickBooks Failure Customer")
    db_session.add(customer)
    db_session.commit()
    _seed_connection(db_session)

    enqueue_sync_customer(db_session, tenant_id=1, customer_id=customer.id)
    db_session.commit()

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/customer"):
            return _response(
                400,
                {
                    "Fault": {
                        "type": "ValidationFault",
                        "Error": [
                            {
                                "code": "6000",
                                "Message": "Validation error",
                                "Detail": "DisplayName is required.",
                            }
                        ],
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="sync_customer")
    assert job.status == "failed"
    assert "DisplayName is required." in (job.error_text or "")
    assert db_session.execute(select(AccountingCustomerMap)).scalars().all() == []
    assert any("6000" in payload for payload in _event_payloads(db_session, event_type="job_failed"))
    assert all("qb-access-token" not in payload for payload in _event_payloads(db_session, event_type="job_failed"))


def test_token_refresh_path_works_with_mocked_responses(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-REFRESH-1", name="Refresh Customer")
    db_session.add(customer)
    db_session.commit()
    connection = _seed_connection(
        db_session,
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        expires_at=datetime.utcnow() - timedelta(minutes=5),
    )

    enqueue_sync_customer(db_session, tenant_id=1, customer_id=customer.id)
    db_session.commit()

    def fake_post(url, data=None, auth=None, headers=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "old-refresh-token"
        return _response(
            200,
            {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
                "x_refresh_token_expires_in": 7200,
                "scope": "com.intuit.quickbooks.accounting",
            },
        )

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer new-access-token"
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/customer"):
            return _response(
                200,
                {
                    "Customer": {
                        "Id": "QB-CUST-REFRESH-1",
                        "SyncToken": "0",
                        "DisplayName": "C-QB-REFRESH-1 - Refresh Customer",
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    monkeypatch.setattr(quickbooks_oauth_module.httpx, "post", fake_post)
    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.succeeded == 1
    db_session.refresh(connection)
    assert connection.status == "connected"
    assert decrypt_string(connection.encrypted_access_token) == "new-access-token"
    assert decrypt_string(connection.encrypted_refresh_token) == "new-refresh-token"


def test_void_job_fails_cleanly_when_unsupported(db_session, monkeypatch):
    _configure_settings(monkeypatch)
    customer = Customer(account_code="C-QB-VOID-1", name="Void Sync Customer")
    invoice = Invoice(
        invoice_no="INV-QB-VOID-1",
        customer_id=1,
        invoice_date=date(2026, 1, 10),
        status="VOID",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(customer)
    db_session.flush()
    invoice.customer_id = customer.id
    db_session.add(invoice)
    db_session.commit()
    _seed_connection(db_session)

    enqueue_void_invoice(db_session, tenant_id=1, invoice_id=invoice.id)
    db_session.commit()

    result = process_pending_accounting_jobs(db_session, tenant_id=1, limit=1)

    assert result.processed == 1
    assert result.failed == 1
    job = _job(db_session, job_type="void_invoice")
    assert job.status == "failed"
    assert "not supported yet" in (job.error_text or "").lower()
