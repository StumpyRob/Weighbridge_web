from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.auth import hash_password
from app.models import (
    AuditEvent,
    Customer,
    DirectionEnum,
    Invoice,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    User,
    PaymentMethod,
)
from app.seed import seed_payment_methods
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


def _csrf_from_cookie(client) -> str:
    csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    return csrf


def test_ticket_complete_creates_audit_event_with_user_and_entity(client, db_session):
    customer = Customer(account_code="C-AUD-TKT-1", name="Audit Ticket Customer")
    unit = Unit(name="Audit Ticket Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-AUD-TKT-1",
        description="Audit Ticket Product",
        unit=unit,
        unit_price=Decimal("50.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-AUD-COMPLETE-1",
        datetime=datetime(2026, 3, 3, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-03-03T10:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
            "unit_price": "50.00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "COMPLETE",
            AuditEvent.entity_type == "ticket",
            AuditEvent.entity_id == str(ticket.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    current_user_id = db_session.execute(select(User.id).order_by(User.id.asc())).scalar_one()
    assert event.user_id == current_user_id
    assert event.entity_id == str(ticket.id)


def test_customer_create_creates_audit_event(client, db_session):
    response = client.post(
        "/customers/new",
        data={
            "account_code": "C-AUD-CUST-1",
            "name": "Audit Customer",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    customer = db_session.execute(
        select(Customer).where(Customer.account_code == "C-AUD-CUST-1")
    ).scalar_one()
    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "CREATE",
            AuditEvent.entity_type == "customer",
            AuditEvent.entity_id == str(customer.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    assert event.summary is not None


def test_invoice_paid_creates_audit_event(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-AUD-INV-1", name="Audit Invoice Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-AUD-PAID-1",
        customer_id=customer.id,
        invoice_date=date(2026, 3, 3),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/paid",
        data={
            "payment_method_id": str(payment_method.id),
            "paid_at": "2026-03-03T11:00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "PAID",
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == str(invoice.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    assert event.user_id is not None


def test_admin_audit_redirects_when_unauthenticated(client_anonymous):
    response = client_anonymous.get("/admin/audit", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers.get("location") == "/login?next=/admin/audit"


def test_operator_cannot_access_admin_audit(client, db_session):
    operator = User(
        username="operator-audit@example.com",
        password_hash=hash_password("OperatorPass123!"),
        is_active=True,
    )
    db_session.add(operator)
    db_session.commit()

    csrf = _csrf_from_cookie(client)
    logout = client.post(
        "/logout",
        data={CSRF_FORM_FIELD: csrf},
        follow_redirects=False,
    )
    assert logout.status_code == 303

    client.get("/login")
    csrf = _csrf_from_cookie(client)
    login = client.post(
        "/login",
        data={
            "email": "operator-audit@example.com",
            "password": "OperatorPass123!",
            "next": "/admin/audit",
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert login.status_code == 303

    forbidden = client.get("/admin/audit")
    assert forbidden.status_code == 403
