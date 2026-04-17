from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import Request
from sqlalchemy import func, select

from app.auth import hash_password, user_identity_kwargs
from app.config import settings
from app.models import (
    AccountingConnection,
    AccountingCustomerMap,
    AccountingInvoiceSync,
    AccountingProductMap,
    AccountingSyncEvent,
    AccountingSyncJob,
    AccountingTaxMap,
    AuditEvent,
    Customer,
    Invoice,
    Product,
    TaxRate,
    Tenant,
    User,
    UserFeedback,
)
from app.models.base import utcnow
from app.services.demo_tenant_reset import (
    DEMO_DEFAULT_EMAIL,
    maybe_auto_reset_demo_tenant,
    reset_demo_tenant_data,
)
from app.services.system_setup import seed_required_reference_data
from app.user_roles import ROLE_TENANT_ADMIN


def _tenant_row_count(db_session, model, tenant_id: int) -> int:
    return int(
        db_session.execute(
            select(func.count(model.id)).where(model.tenant_id == int(tenant_id))
        ).scalar_one()
        or 0
    )


def _demo_request() -> Request:
    host = f"{settings.effective_demo_tenant_subdomain}.localhost"
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"host", host.encode("ascii"))],
            "client": ("127.0.0.1", 50000),
            "server": (host, 443),
        }
    )


def _seed_demo_tenant_with_accounting_rows(db_session) -> dict[str, int]:
    seed_required_reference_data(db_session)

    tenant = Tenant(
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    db_session.add(tenant)
    db_session.flush()

    tax_rate = (
        db_session.execute(
            select(TaxRate).where(TaxRate.code.like("Standard (20%)%")).limit(1)
        )
        .scalars()
        .first()
    )
    assert tax_rate is not None

    customer = Customer(
        tenant_id=tenant.id,
        account_code="DEMO-STALE-CUST",
        name="Stale Demo Customer",
    )
    product = Product(
        tenant_id=tenant.id,
        code="DEMO-STALE-PROD",
        description="Stale Demo Product",
        nominal_code="4000",
        unit_price=Decimal("10.00"),
        tax_rate_id=tax_rate.id,
    )
    user = User(
        **user_identity_kwargs(email="stale-demo@example.com", role=ROLE_TENANT_ADMIN),
        password_hash=hash_password("DemoPass123!"),
        is_active=True,
        tenant_id=tenant.id,
    )
    db_session.add_all([customer, product, user])
    db_session.flush()

    invoice = Invoice(
        tenant_id=tenant.id,
        invoice_no="DEMO-INV-001",
        customer_id=customer.id,
        invoice_date=date(2026, 4, 17),
        status="ISSUED",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(invoice)
    db_session.flush()

    db_session.add_all(
        [
            AccountingConnection(
                tenant_id=tenant.id,
                provider="quickbooks",
                status="connected",
                realm_id="realm-demo",
            ),
            AccountingCustomerMap(
                tenant_id=tenant.id,
                provider="quickbooks",
                customer_id=customer.id,
                external_id="QB-CUST-DEMO",
                sync_status="synced",
            ),
            AccountingProductMap(
                tenant_id=tenant.id,
                provider="quickbooks",
                product_id=product.id,
                external_id="QB-PROD-DEMO",
                sync_status="synced",
            ),
            AccountingInvoiceSync(
                tenant_id=tenant.id,
                provider="quickbooks",
                invoice_id=invoice.id,
                external_id="QB-INV-DEMO",
                external_doc_number="1001",
                sync_status="synced",
            ),
            AccountingSyncJob(
                tenant_id=tenant.id,
                provider="quickbooks",
                job_type="sync_invoice",
                entity_type="invoice",
                entity_id=invoice.id,
                status="pending",
            ),
            AccountingSyncEvent(
                tenant_id=tenant.id,
                provider="quickbooks",
                event_type="invoice_synced",
                entity_type="invoice",
                entity_id=invoice.id,
                direction="OUTBOUND",
                summary="Demo invoice synced",
            ),
            AccountingTaxMap(
                tenant_id=tenant.id,
                provider="quickbooks",
                tax_rate_id=tax_rate.id,
                external_id="QB-TAX-DEMO",
                external_code="TAX",
                is_active=True,
            ),
            UserFeedback(
                tenant_id=tenant.id,
                submitted_by_user_id=user.id,
                reviewed_by_user_id=user.id,
                kind="bug",
                status="reviewed",
                title="Stale demo feedback",
                message="This stale feedback row should be deleted during demo reset.",
                submitted_by_display_name="Stale Demo Admin",
                submitted_by_email="stale-demo@example.com",
            ),
        ]
    )
    db_session.commit()
    return {
        "tenant_id": int(tenant.id),
        "user_id": int(user.id),
        "customer_id": int(customer.id),
        "product_id": int(product.id),
        "invoice_id": int(invoice.id),
        "customer_account_code": str(customer.account_code),
        "product_code": str(product.code),
        "invoice_no": str(invoice.invoice_no),
        "user_email": str(user.email),
    }


def test_reset_demo_tenant_data_clears_accounting_rows_before_core_entities(
    db_session,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))
    ids = _seed_demo_tenant_with_accounting_rows(db_session)
    tenant = db_session.get(Tenant, ids["tenant_id"])
    assert tenant is not None

    reset_demo_tenant_data(
        db_session,
        None,
        tenant=tenant,
        current_user=None,
        reset_reason="automatic",
    )

    for model in (
        AccountingConnection,
        AccountingCustomerMap,
        AccountingProductMap,
        AccountingInvoiceSync,
        AccountingSyncJob,
        AccountingSyncEvent,
        AccountingTaxMap,
        UserFeedback,
    ):
        assert _tenant_row_count(db_session, model, ids["tenant_id"]) == 0

    assert (
        db_session.execute(
            select(Customer).where(
                Customer.tenant_id == ids["tenant_id"],
                Customer.account_code == ids["customer_account_code"],
            )
        )
        .scalars()
        .first()
        is None
    )
    assert (
        db_session.execute(
            select(Product).where(
                Product.tenant_id == ids["tenant_id"],
                Product.code == ids["product_code"],
            )
        )
        .scalars()
        .first()
        is None
    )
    assert (
        db_session.execute(
            select(Invoice).where(
                Invoice.tenant_id == ids["tenant_id"],
                Invoice.invoice_no == ids["invoice_no"],
            )
        )
        .scalars()
        .first()
        is None
    )
    assert (
        db_session.execute(
            select(User).where(
                User.tenant_id == ids["tenant_id"],
                User.email == ids["user_email"],
            )
        )
        .scalars()
        .first()
        is None
    )

    default_demo_user = (
        db_session.execute(
            select(User).where(
                User.tenant_id == ids["tenant_id"],
                User.email == DEMO_DEFAULT_EMAIL,
            )
        )
        .scalars()
        .first()
    )
    assert default_demo_user is not None

    reset_event = (
        db_session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "TENANT_RESET_DEMO",
                AuditEvent.entity_id == str(ids["tenant_id"]),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    assert reset_event is not None


def test_maybe_auto_reset_demo_tenant_handles_accounting_rows_when_reset_is_due(
    db_session,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))
    ids = _seed_demo_tenant_with_accounting_rows(db_session)
    tenant = db_session.get(Tenant, ids["tenant_id"])
    assert tenant is not None
    tenant.demo_reset_interval_days = 1
    tenant.demo_reset_time_minutes = 0
    tenant.demo_last_reset_at = utcnow() - timedelta(days=2)
    db_session.commit()

    was_reset = maybe_auto_reset_demo_tenant(
        db_session,
        _demo_request(),
        tenant=tenant,
    )

    assert was_reset is True
    assert _tenant_row_count(db_session, AccountingInvoiceSync, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingCustomerMap, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingProductMap, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingSyncJob, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingSyncEvent, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingConnection, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, AccountingTaxMap, ids["tenant_id"]) == 0
    assert _tenant_row_count(db_session, UserFeedback, ids["tenant_id"]) == 0
    assert (
        db_session.execute(
            select(Invoice).where(
                Invoice.tenant_id == ids["tenant_id"],
                Invoice.invoice_no == ids["invoice_no"],
            )
        )
        .scalars()
        .first()
        is None
    )
