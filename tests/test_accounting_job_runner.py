import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import select

import app.services.accounting.invoice_sync as invoice_sync_module
import app.services.accounting.quickbooks_client as quickbooks_client_module
import app.services.accounting.quickbooks_oauth as quickbooks_oauth_module
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
from app.services.accounting.revenue_account_mapping import list_provider_revenue_accounts
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
        external_id="QB-TAX-GROUP-20",
        external_code="TAX",
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
        external_id="QB-TAX-GROUP-20",
        external_code="TAX",
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
        external_id="QB-TAX-GROUP-20",
        external_code="TAX",
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
        if url.endswith("/query"):
            return _response(200, {"QueryResponse": {}})
        if url.endswith("/invoice"):
            assert json["DocNumber"] == "INV-QB-1"
            assert json["Line"][0]["Amount"] == 10.0
            assert json["Line"][0]["SalesItemLineDetail"]["TaxCodeRef"]["value"] == "TAX"
            assert json["TxnTaxDetail"]["TxnTaxCodeRef"]["value"] == "QB-TAX-GROUP-20"
            assert "GlobalTaxCalculation" not in json
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
    assert sync_row.provider_response_json["txn_tax_code_ref"] == "QB-TAX-GROUP-20"
    assert sync_row.provider_response_json["local_totals"]["gross_total"] == 12.0
    assert _job(db_session, job_type="sync_invoice").status == "succeeded"


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
        external_id="QB-TAX-GROUP-20",
        external_code="TAX",
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
