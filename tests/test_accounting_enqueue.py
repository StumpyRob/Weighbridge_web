from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import app.services.accounting.quickbooks_oauth as quickbooks_oauth
from sqlalchemy import select

from app.models import (
    AccountingConnection,
    AccountingSyncEvent,
    AccountingSyncJob,
    Customer,
    DirectionEnum,
    Invoice,
    PaymentMethod,
    Product,
    TaxRate,
    Tenant,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    VoidReason,
)
from app.seed import VOID_REASON_TYPE_INVOICE, seed_invoice_void_reasons, seed_payment_methods
from app.services.accounting.jobs import enqueue_sync_customer


def _active_connection(
    db_session,
    *,
    tenant_id: int = 1,
    provider: str = "quickbooks",
) -> AccountingConnection:
    connection = AccountingConnection(
        tenant_id=tenant_id,
        provider=provider,
        status="connected",
        realm_id=f"realm-{tenant_id}",
        encrypted_access_token="enc-access",
        encrypted_refresh_token="enc-refresh",
    )
    db_session.add(connection)
    db_session.commit()
    return connection


def _jobs(
    db_session,
    *,
    job_type: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    tenant_id: int | None = None,
) -> list[AccountingSyncJob]:
    query = select(AccountingSyncJob).order_by(AccountingSyncJob.id.asc())
    if job_type:
        query = query.where(AccountingSyncJob.job_type == job_type)
    if entity_type:
        query = query.where(AccountingSyncJob.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AccountingSyncJob.entity_id == entity_id)
    if tenant_id is not None:
        query = query.where(AccountingSyncJob.tenant_id == tenant_id)
    return list(db_session.execute(query).scalars())


def _events(
    db_session,
    *,
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    tenant_id: int | None = None,
) -> list[AccountingSyncEvent]:
    query = select(AccountingSyncEvent).order_by(AccountingSyncEvent.id.asc())
    if event_type:
        query = query.where(AccountingSyncEvent.event_type == event_type)
    if entity_type:
        query = query.where(AccountingSyncEvent.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AccountingSyncEvent.entity_id == entity_id)
    if tenant_id is not None:
        query = query.where(AccountingSyncEvent.tenant_id == tenant_id)
    return list(db_session.execute(query).scalars())


def _invoiceable_ticket(
    db_session,
    *,
    customer_id: int,
    ticket_no: str,
    when: datetime,
) -> Ticket:
    unit = Unit(name=f"Queue Unit {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"QUEUE-PROD-{ticket_no}",
        description=f"Queue Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=when,
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer_id,
        product=product,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("10.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()
    return ticket


def _draft_invoice(
    db_session,
    *,
    customer_id: int,
    invoice_no: str,
) -> Invoice:
    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer_id,
        invoice_date=date(2026, 1, 10),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_customer_create_enqueues_sync_job_only_when_connection_active(
    client, db_session, monkeypatch
):
    def _unexpected_http_call(*args, **kwargs):
        raise AssertionError("Provider HTTP should not be called during enqueue.")

    monkeypatch.setattr(quickbooks_oauth.httpx, "post", _unexpected_http_call)

    without_connection = client.post(
        "/customers/new",
        data={
            "account_code": "C-QUEUE-1",
            "name": "Queue Customer One",
        },
        follow_redirects=False,
    )
    assert without_connection.status_code == 303
    assert _jobs(db_session, job_type="sync_customer") == []

    _active_connection(db_session)

    with_connection = client.post(
        "/customers/new",
        data={
            "account_code": "C-QUEUE-2",
            "name": "Queue Customer Two",
        },
        follow_redirects=False,
    )
    assert with_connection.status_code == 303

    customer = db_session.execute(
        select(Customer).where(Customer.account_code == "C-QUEUE-2")
    ).scalar_one()
    jobs = _jobs(
        db_session,
        job_type="sync_customer",
        entity_type="customer",
        entity_id=customer.id,
    )
    assert len(jobs) == 1
    assert jobs[0].tenant_id == customer.tenant_id
    assert jobs[0].provider == "quickbooks"
    assert jobs[0].status == "pending"
    assert jobs[0].attempts == 0

    events = _events(
        db_session,
        event_type="job_enqueued",
        entity_type="customer",
        entity_id=customer.id,
    )
    assert len(events) == 1
    assert events[0].summary == "Queued accounting customer sync"


def test_customer_update_enqueues_sync_job_only_when_connection_active(client, db_session):
    customer = Customer(account_code="C-QUEUE-UPD", name="Queue Update Customer")
    db_session.add(customer)
    db_session.commit()

    without_connection = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": customer.account_code,
            "name": "Queue Update Customer One",
            "postcode": "AB1 2CD",
        },
        follow_redirects=False,
    )
    assert without_connection.status_code == 303
    assert _jobs(
        db_session,
        job_type="sync_customer",
        entity_type="customer",
        entity_id=customer.id,
    ) == []

    _active_connection(db_session)

    with_connection = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": customer.account_code,
            "name": "Queue Update Customer Two",
            "postcode": "AB1 3CD",
        },
        follow_redirects=False,
    )
    assert with_connection.status_code == 303

    jobs = _jobs(
        db_session,
        job_type="sync_customer",
        entity_type="customer",
        entity_id=customer.id,
    )
    assert len(jobs) == 1
    assert jobs[0].tenant_id == customer.tenant_id


def test_product_create_and_update_enqueue_correctly(client, db_session):
    tax_rate = TaxRate(
        code="QUEUE VAT",
        description="Queue VAT",
        rate_percent=Decimal("0.200"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    create_without_connection = client.post(
        "/products/new",
        data={
            "code": "QUEUE-PROD-1",
            "description": "Queue Product One",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "20.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
        },
        follow_redirects=False,
    )
    assert create_without_connection.status_code == 303
    product_one = db_session.execute(
        select(Product).where(Product.code == "QUEUE-PROD-1")
    ).scalar_one()
    assert _jobs(
        db_session,
        job_type="sync_product",
        entity_type="product",
        entity_id=product_one.id,
    ) == []

    _active_connection(db_session)

    create_with_connection = client.post(
        "/products/new",
        data={
            "code": "QUEUE-PROD-2",
            "description": "Queue Product Two",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "25.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
        },
        follow_redirects=False,
    )
    assert create_with_connection.status_code == 303

    product_two = db_session.execute(
        select(Product).where(Product.code == "QUEUE-PROD-2")
    ).scalar_one()
    create_jobs = _jobs(
        db_session,
        job_type="sync_product",
        entity_type="product",
        entity_id=product_two.id,
    )
    assert len(create_jobs) == 1
    assert create_jobs[0].tenant_id == product_two.tenant_id

    update_with_connection = client.post(
        f"/products/{product_two.id}",
        data={
            "code": "QUEUE-PROD-2",
            "description": "Queue Product Two Updated",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "30.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
        },
        follow_redirects=False,
    )
    assert update_with_connection.status_code == 303

    update_jobs = _jobs(
        db_session,
        job_type="sync_product",
        entity_type="product",
        entity_id=product_two.id,
    )
    assert len(update_jobs) == 1


def test_invoice_generate_confirm_enqueues_sync_invoice_only_when_connection_active(
    client, db_session
):
    customer_one = Customer(account_code="C-QUEUE-INV-1", name="Invoice Queue One")
    db_session.add(customer_one)
    db_session.commit()
    _invoiceable_ticket(
        db_session,
        customer_id=customer_one.id,
        ticket_no="T-QUEUE-INV-1",
        when=datetime(2026, 2, 12, 10, 0, 0),
    )

    without_connection = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer_one.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )
    assert without_connection.status_code == 303
    invoice_one = db_session.execute(
        select(Invoice).where(Invoice.customer_id == customer_one.id).order_by(Invoice.id.desc())
    ).scalar_one()
    assert _jobs(
        db_session,
        job_type="sync_invoice",
        entity_type="invoice",
        entity_id=invoice_one.id,
    ) == []

    _active_connection(db_session)

    customer_two = Customer(account_code="C-QUEUE-INV-2", name="Invoice Queue Two")
    db_session.add(customer_two)
    db_session.commit()
    _invoiceable_ticket(
        db_session,
        customer_id=customer_two.id,
        ticket_no="T-QUEUE-INV-2",
        when=datetime(2026, 2, 13, 10, 0, 0),
    )

    with_connection = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer_two.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )
    assert with_connection.status_code == 303

    invoice_two = db_session.execute(
        select(Invoice).where(Invoice.customer_id == customer_two.id).order_by(Invoice.id.desc())
    ).scalar_one()
    jobs = _jobs(
        db_session,
        job_type="sync_invoice",
        entity_type="invoice",
        entity_id=invoice_two.id,
    )
    assert len(jobs) == 1
    assert jobs[0].tenant_id == invoice_two.tenant_id


def test_invoice_mark_paid_enqueues_only_when_connection_active(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()

    customer_one = Customer(account_code="C-QUEUE-PAID-1", name="Paid Queue One")
    db_session.add(customer_one)
    db_session.commit()
    invoice_one = _draft_invoice(
        db_session,
        customer_id=customer_one.id,
        invoice_no="INV-QUEUE-PAID-1",
    )

    without_connection = client.post(
        f"/invoices/{invoice_one.id}/paid",
        data={
            "payment_method_id": str(payment_method.id),
            "paid_at": "2026-01-11",
        },
        follow_redirects=False,
    )
    assert without_connection.status_code == 303
    assert _jobs(
        db_session,
        job_type="mark_invoice_paid",
        entity_type="invoice",
        entity_id=invoice_one.id,
    ) == []

    _active_connection(db_session)

    customer_two = Customer(account_code="C-QUEUE-PAID-2", name="Paid Queue Two")
    db_session.add(customer_two)
    db_session.commit()
    invoice_two = _draft_invoice(
        db_session,
        customer_id=customer_two.id,
        invoice_no="INV-QUEUE-PAID-2",
    )

    with_connection = client.post(
        f"/invoices/{invoice_two.id}/paid",
        data={
            "payment_method_id": str(payment_method.id),
            "paid_at": "2026-01-12",
        },
        follow_redirects=False,
    )
    assert with_connection.status_code == 303

    jobs = _jobs(
        db_session,
        job_type="mark_invoice_paid",
        entity_type="invoice",
        entity_id=invoice_two.id,
    )
    assert len(jobs) == 1
    assert jobs[0].tenant_id == invoice_two.tenant_id


def test_invoice_void_enqueues_when_connection_active(client, db_session):
    seed_invoice_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
        .limit(1)
    ).scalar_one()
    _active_connection(db_session)

    customer = Customer(account_code="C-QUEUE-VOID-1", name="Void Queue Customer")
    db_session.add(customer)
    db_session.commit()
    invoice = _draft_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-QUEUE-VOID-1",
    )

    response = client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_reason_id": str(reason.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    jobs = _jobs(
        db_session,
        job_type="void_invoice",
        entity_type="invoice",
        entity_id=invoice.id,
    )
    assert len(jobs) == 1
    assert jobs[0].tenant_id == invoice.tenant_id


def test_accounting_enqueue_preserves_tenant_isolation(db_session):
    tenant_one = Tenant(name="Queue Tenant One", subdomain="queue-one")
    tenant_two = Tenant(name="Queue Tenant Two", subdomain="queue-two")
    db_session.add_all([tenant_one, tenant_two])
    db_session.flush()

    customer_one = Customer(
        tenant_id=tenant_one.id,
        account_code="C-TENANT-ONE",
        name="Tenant One Customer",
    )
    customer_two = Customer(
        tenant_id=tenant_two.id,
        account_code="C-TENANT-TWO",
        name="Tenant Two Customer",
    )
    db_session.add_all([customer_one, customer_two])
    db_session.flush()

    db_session.add(
        AccountingConnection(
            tenant_id=tenant_one.id,
            provider="quickbooks",
            status="connected",
            realm_id="realm-tenant-one",
            encrypted_access_token="enc-access",
            encrypted_refresh_token="enc-refresh",
        )
    )
    db_session.commit()

    job_one = enqueue_sync_customer(
        db_session,
        tenant_id=tenant_one.id,
        customer_id=customer_one.id,
    )
    job_two = enqueue_sync_customer(
        db_session,
        tenant_id=tenant_two.id,
        customer_id=customer_two.id,
    )
    db_session.commit()

    assert job_one is not None
    assert job_one.tenant_id == tenant_one.id
    assert job_two is None

    tenant_one_jobs = _jobs(
        db_session,
        job_type="sync_customer",
        tenant_id=tenant_one.id,
    )
    tenant_two_jobs = _jobs(
        db_session,
        job_type="sync_customer",
        tenant_id=tenant_two.id,
    )
    assert len(tenant_one_jobs) == 1
    assert tenant_two_jobs == []
