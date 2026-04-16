from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import TENANT_FILTER_MODELS, TenantSession
from app.models import (
    AccountingConnection,
    AccountingCustomerMap,
    AccountingInvoiceSync,
    AccountingProductMap,
    AccountingSyncEvent,
    AccountingSyncJob,
    AccountingTaxMap,
    Customer,
    Invoice,
    Product,
    TaxRate,
    Tenant,
)


def _sqlite_unique_sets(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        unique_sets: set[tuple[str, ...]] = set()
        for row in conn.execute(f"PRAGMA index_list('{table_name}')").fetchall():
            index_name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            columns = tuple(
                info_row[2]
                for info_row in conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
            )
            unique_sets.add(columns)
        return unique_sets
    finally:
        conn.close()


def _sqlite_columns(db_path: Path, table_name: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {str(row[1]) for row in rows}
    finally:
        conn.close()


def _seed_accounting_entities(db_session):
    tenant = Tenant(name="Tenant One", subdomain="tenant-one")
    other_tenant = Tenant(name="Tenant Two", subdomain="tenant-two")
    db_session.add_all([tenant, other_tenant])
    db_session.flush()

    customer = Customer(tenant_id=tenant.id, account_code="CUST001", name="Customer One")
    customer_two = Customer(
        tenant_id=tenant.id,
        account_code="CUST002",
        name="Customer Two",
    )
    tax_rate = TaxRate(code="STD20", description="Standard 20%", rate_percent=Decimal("20.000"))
    tax_rate_two = TaxRate(code="ZERO", description="Zero", rate_percent=Decimal("0.000"))
    product = Product(
        tenant_id=tenant.id,
        code="PROD001",
        description="Product One",
        unit_price=Decimal("10.00"),
        tax_rate=tax_rate,
    )
    product_two = Product(
        tenant_id=tenant.id,
        code="PROD002",
        description="Product Two",
        unit_price=Decimal("20.00"),
        tax_rate=tax_rate_two,
    )
    db_session.add_all([customer, customer_two, tax_rate, tax_rate_two, product, product_two])
    db_session.flush()

    invoice = Invoice(
        tenant_id=tenant.id,
        invoice_no="INV001",
        customer_id=customer.id,
        invoice_date=date(2026, 4, 16),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    return {
        "tenant_id": int(tenant.id),
        "other_tenant_id": int(other_tenant.id),
        "customer_id": int(customer.id),
        "customer_two_id": int(customer_two.id),
        "product_id": int(product.id),
        "product_two_id": int(product_two.id),
        "invoice_id": int(invoice.id),
    }


def test_head_migration_creates_accounting_foundation_tables(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))

    with tempfile.TemporaryDirectory(dir=root) as tmpdir:
        db_path = Path(tmpdir) / "accounting-foundation.sqlite3"
        db_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
        monkeypatch.setenv("database_url", db_url)
        monkeypatch.setenv("secret_key", "accounting-foundation-test-secret")
        monkeypatch.setattr(settings, "database_url", db_url)

        command.upgrade(cfg, "head")

        connection_columns = _sqlite_columns(db_path, "accounting_connections")
        assert {
            "tenant_id",
            "provider",
            "status",
            "realm_id",
            "encrypted_access_token",
            "encrypted_refresh_token",
        } <= connection_columns

        job_columns = _sqlite_columns(db_path, "accounting_sync_jobs")
        assert {
            "job_type",
            "entity_type",
            "entity_id",
            "status",
            "attempts",
            "available_at",
            "lock_token",
            "payload_json",
        } <= job_columns

        event_columns = _sqlite_columns(db_path, "accounting_sync_events")
        assert {"event_type", "direction", "summary", "detail_json"} <= event_columns

        tax_map_columns = _sqlite_columns(db_path, "accounting_tax_maps")
        assert {
            "tenant_id",
            "provider",
            "tax_rate_id",
            "external_id",
            "external_code",
            "is_active",
        } <= tax_map_columns

        connection_uniques = _sqlite_unique_sets(db_path, "accounting_connections")
        assert ("tenant_id", "provider") in connection_uniques

        customer_map_uniques = _sqlite_unique_sets(db_path, "accounting_customer_maps")
        assert ("tenant_id", "provider", "customer_id") in customer_map_uniques
        assert ("tenant_id", "provider", "external_id") in customer_map_uniques

        product_map_uniques = _sqlite_unique_sets(db_path, "accounting_product_maps")
        assert ("tenant_id", "provider", "product_id") in product_map_uniques
        assert ("tenant_id", "provider", "external_id") in product_map_uniques

        tax_map_uniques = _sqlite_unique_sets(db_path, "accounting_tax_maps")
        assert ("tenant_id", "provider", "tax_rate_id") in tax_map_uniques

        invoice_sync_uniques = _sqlite_unique_sets(db_path, "accounting_invoice_syncs")
        assert ("tenant_id", "provider", "invoice_id") in invoice_sync_uniques


def test_accounting_models_are_tenant_scoped(engine) -> None:
    accounting_models = {
        AccountingConnection,
        AccountingCustomerMap,
        AccountingInvoiceSync,
        AccountingProductMap,
        AccountingSyncEvent,
        AccountingSyncJob,
        AccountingTaxMap,
    }
    assert accounting_models <= set(TENANT_FILTER_MODELS)

    PlainSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with PlainSessionLocal() as db:
        tenant_one = Tenant(name="Tenant One", subdomain="tenant-one")
        tenant_two = Tenant(name="Tenant Two", subdomain="tenant-two")
        db.add_all([tenant_one, tenant_two])
        db.flush()
        db.add_all(
            [
                AccountingConnection(
                    tenant_id=tenant_one.id,
                    provider="quickbooks",
                    status="connected",
                ),
                AccountingConnection(
                    tenant_id=tenant_two.id,
                    provider="quickbooks",
                    status="connected",
                ),
            ]
        )
        db.commit()
        tenant_one_id = int(tenant_one.id)
        tenant_two_id = int(tenant_two.id)

    ScopedSessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        class_=TenantSession,
    )
    with ScopedSessionLocal() as db:
        db.info["tenant_id"] = tenant_one_id
        db.info["platform_mode"] = False
        tenant_one_rows = db.execute(
            select(AccountingConnection).order_by(AccountingConnection.id.asc())
        ).scalars().all()
        assert len(tenant_one_rows) == 1
        assert int(tenant_one_rows[0].tenant_id) == tenant_one_id

    with ScopedSessionLocal() as db:
        db.info["tenant_id"] = tenant_two_id
        db.info["platform_mode"] = False
        tenant_two_rows = db.execute(
            select(AccountingConnection).order_by(AccountingConnection.id.asc())
        ).scalars().all()
        assert len(tenant_two_rows) == 1
        assert int(tenant_two_rows[0].tenant_id) == tenant_two_id


def test_accounting_connection_unique_per_tenant_provider(db_session) -> None:
    ids = _seed_accounting_entities(db_session)
    db_session.add(
        AccountingConnection(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            status="connected",
        )
    )
    db_session.commit()

    db_session.add(
        AccountingConnection(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            status="disconnected",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_accounting_customer_map_unique_constraints(db_session) -> None:
    ids = _seed_accounting_entities(db_session)
    db_session.add(
        AccountingCustomerMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            customer_id=ids["customer_id"],
            external_id="qb-customer-1",
            sync_status="synced",
        )
    )
    db_session.commit()

    db_session.add(
        AccountingCustomerMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            customer_id=ids["customer_id"],
            external_id="qb-customer-2",
            sync_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        AccountingCustomerMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            customer_id=ids["customer_two_id"],
            external_id="qb-customer-1",
            sync_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_accounting_product_map_unique_constraints(db_session) -> None:
    ids = _seed_accounting_entities(db_session)
    db_session.add(
        AccountingProductMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            product_id=ids["product_id"],
            external_id="qb-item-1",
            sync_status="synced",
        )
    )
    db_session.commit()

    db_session.add(
        AccountingProductMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            product_id=ids["product_id"],
            external_id="qb-item-2",
            sync_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        AccountingProductMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            product_id=ids["product_two_id"],
            external_id="qb-item-1",
            sync_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_accounting_invoice_sync_unique_per_invoice_provider(db_session) -> None:
    ids = _seed_accounting_entities(db_session)
    db_session.add(
        AccountingInvoiceSync(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            invoice_id=ids["invoice_id"],
            external_id="qb-invoice-1",
            external_doc_number="1001",
            sync_status="synced",
        )
    )
    db_session.commit()

    db_session.add(
        AccountingInvoiceSync(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            invoice_id=ids["invoice_id"],
            external_id="qb-invoice-2",
            external_doc_number="1002",
            sync_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_accounting_tax_map_unique_per_tax_rate_provider(db_session) -> None:
    ids = _seed_accounting_entities(db_session)
    tax_rate = db_session.execute(
        select(TaxRate).where(TaxRate.code == "STD20")
    ).scalar_one()
    tax_rate_two = db_session.execute(
        select(TaxRate).where(TaxRate.code == "ZERO")
    ).scalar_one()
    db_session.add(
        AccountingTaxMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            tax_rate_id=tax_rate.id,
            external_id="QB-TAX-20",
            external_code="TAX",
            is_active=True,
        )
    )
    db_session.commit()

    db_session.add(
        AccountingTaxMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            tax_rate_id=tax_rate.id,
            external_id="QB-TAX-20-DUP",
            external_code="TAX",
            is_active=True,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        AccountingTaxMap(
            tenant_id=ids["tenant_id"],
            provider="quickbooks",
            tax_rate_id=tax_rate_two.id,
            external_code="NON",
            is_active=True,
        )
    )
    db_session.commit()
