from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select

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

SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR4nGP4DwQMDAz/ARruBPywhCTXAAAAAElFTkSuQmCC"
)
BLANK_SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAC0lEQVR4nGP4DwUAI+UH+Yo0eLMAAAAASUVORK5CYII="
)


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
    changed = (event.details_json or {}).get("changed", {})
    assert changed.get("status", {}).get("to") == TicketStatusEnum.COMPLETE.value


def test_ticket_complete_logs_exactly_one_event_on_repeat_attempt(client, db_session):
    customer = Customer(account_code="C-AUD-TKT-2", name="Audit Ticket Customer 2")
    unit = Unit(name="Audit Ticket Unit 2", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-AUD-TKT-2",
        description="Audit Ticket Product 2",
        unit=unit,
        unit_price=Decimal("40.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-AUD-COMPLETE-2",
        datetime=datetime(2026, 3, 3, 10, 30, 0),
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

    first = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-03-03T10:30",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
            "unit_price": "40.00",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-03-03T10:30",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
            "unit_price": "40.00",
        },
        follow_redirects=False,
    )
    assert second.status_code == 400

    count = db_session.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "COMPLETE",
            AuditEvent.entity_type == "ticket",
            AuditEvent.entity_id == str(ticket.id),
        )
    ).scalar_one()
    assert int(count) == 1


def test_ticket_save_operation_flag_changes_are_audited(client, db_session):
    customer = Customer(account_code="C-AUD-TKT-3", name="Audit Ticket Customer 3")
    unit = Unit(name="Audit Ticket Unit 3", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-AUD-TKT-3",
        description="Audit Ticket Product 3",
        unit=unit,
        unit_price=Decimal("25.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-AUD-SAVE-1",
        datetime=datetime(2026, 3, 3, 12, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        final_disposal=True,
        used_on_site=False,
        qty=Decimal("1"),
        unit_price=Decimal("25.00"),
        total=Decimal("25.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-03-03T12:00",
            "direction": "OUTWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "final_disposal": "",
            "used_on_site": "on",
            "qty": "1",
            "unit_price": "25.00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "UPDATE",
            AuditEvent.entity_type == "ticket",
            AuditEvent.entity_id == str(ticket.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    changed = (event.details_json or {}).get("changed", {})
    assert changed.get("final_disposal") == {"from": True, "to": False}
    assert changed.get("used_on_site") == {"from": False, "to": True}


def test_ticket_wtn_signature_save_and_replace_are_audited(client, db_session):
    ticket = Ticket(
        ticket_no="T-AUD-WTN-SIGN-1",
        datetime=datetime(2026, 3, 22, 12, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Audit WTN signature",
        net_kg=Decimal("1200.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    first = client.post(
        f"/tickets/{ticket.id}/wtn/signature/producer",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "blank_signature_data_url": BLANK_SIGNATURE_DATA_URL,
            "signer_name": "First Signer",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = client.post(
        f"/tickets/{ticket.id}/wtn/signature/producer",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "blank_signature_data_url": BLANK_SIGNATURE_DATA_URL,
            "signer_name": "Second Signer",
        },
        follow_redirects=False,
    )
    assert second.status_code == 303

    events = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "TICKET_WTN_SIGNATURE_SAVED",
            AuditEvent.entity_type == "ticket",
            AuditEvent.entity_id == str(ticket.id),
        )
        .order_by(AuditEvent.id.asc())
    ).scalars().all()
    assert len(events) == 2

    first_details = events[0].details_json or {}
    assert first_details.get("ticket_id") == ticket.id
    assert first_details.get("ticket_no") == ticket.ticket_no
    assert first_details.get("role") == "producer"
    assert first_details.get("operation") == "save"
    assert first_details.get("signer_name") == "First Signer"
    assert first_details.get("signed_at")

    second_details = events[1].details_json or {}
    assert second_details.get("role") == "producer"
    assert second_details.get("operation") == "replace"
    assert second_details.get("signer_name") == "Second Signer"
    assert second_details.get("signed_at")


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


def test_invoice_paid_logs_exactly_one_event_on_repeat_attempt(client, db_session):
    seed_payment_methods(db_session)
    payment_method = db_session.execute(
        select(PaymentMethod).where(PaymentMethod.is_active.is_(True)).limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-AUD-INV-2", name="Audit Invoice Customer 2")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-AUD-PAID-2",
        customer_id=customer.id,
        invoice_date=date(2026, 3, 3),
        status="DRAFT",
        net_total=Decimal("10.00"),
        vat_total=Decimal("2.00"),
        gross_total=Decimal("12.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    first = client.post(
        f"/invoices/{invoice.id}/paid",
        data={
            "payment_method_id": str(payment_method.id),
            "paid_at": "2026-03-03T11:30",
        },
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = client.post(
        f"/invoices/{invoice.id}/paid",
        data={
            "payment_method_id": str(payment_method.id),
            "paid_at": "2026-03-03T11:30",
        },
        follow_redirects=False,
    )
    assert second.status_code == 400

    count = db_session.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "PAID",
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == str(invoice.id),
        )
    ).scalar_one()
    assert int(count) == 1


def test_login_success_is_audited_with_user_and_ip(client_anonymous, db_session):
    user = User(
        username="login-audit@example.com",
        password_hash=hash_password("LoginPass123!"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    client_anonymous.get("/login")
    csrf = _csrf_from_cookie(client_anonymous)
    response = client_anonymous.post(
        "/login",
        data={
            "email": "login-audit@example.com",
            "password": "LoginPass123!",
            "next": "/tickets",
            CSRF_FORM_FIELD: csrf,
        },
        headers={"x-forwarded-for": "203.0.113.10"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "LOGIN_SUCCESS",
            AuditEvent.entity_type == "user",
            AuditEvent.entity_id == str(user.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    assert event.user_id == user.id
    assert event.ip_address == "203.0.113.10"


def test_login_failed_is_audited_without_user_match(client_anonymous, db_session):
    user = User(
        username="login-fail-audit@example.com",
        password_hash=hash_password("LoginPass123!"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    client_anonymous.get("/login")
    csrf = _csrf_from_cookie(client_anonymous)
    response = client_anonymous.post(
        "/login",
        data={
            "email": "login-fail-audit@example.com",
            "password": "wrong-password",
            "next": "/tickets",
            CSRF_FORM_FIELD: csrf,
        },
        headers={"x-real-ip": "198.51.100.15"},
        follow_redirects=False,
    )
    assert response.status_code == 401

    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "LOGIN_FAILED",
            AuditEvent.entity_type == "auth",
            AuditEvent.entity_id == "login-fail-audit@example.com",
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    assert event.user_id is None
    assert event.ip_address == "198.51.100.15"
    assert event.details_json == {"reason": "invalid_credentials"}


def test_logout_is_audited_with_user_and_ip(client, db_session):
    current_user_id = db_session.execute(select(User.id).order_by(User.id.asc())).scalar_one()
    csrf = _csrf_from_cookie(client)
    response = client.post(
        "/logout",
        data={CSRF_FORM_FIELD: csrf},
        headers={"x-forwarded-for": "192.0.2.44"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "LOGOUT",
            AuditEvent.entity_type == "user",
            AuditEvent.entity_id == str(current_user_id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    assert event.user_id == current_user_id
    assert event.ip_address == "192.0.2.44"


def test_bootstrap_user_create_is_audited(client_anonymous, db_session):
    bootstrap_page = client_anonymous.get("/bootstrap")
    assert bootstrap_page.status_code == 200
    csrf = _csrf_from_cookie(client_anonymous)
    response = client_anonymous.post(
        "/bootstrap",
        data={
            "email": "bootstrap-audit@example.com",
            "password": "BootstrapPass123!",
            "confirm_password": "BootstrapPass123!",
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    created_user = db_session.execute(
        select(User).where(User.username == "bootstrap-audit@example.com")
    ).scalar_one()
    event = db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == "USER_CREATE",
            AuditEvent.entity_type == "user",
            AuditEvent.entity_id == str(created_user.id),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    ).scalar_one()
    changed = (event.details_json or {}).get("changed", {})
    assert changed.get("is_active", {}).get("to") is True


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
