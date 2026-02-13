from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _make_invoiceable_ticket(db_session, *, customer_id: int, ticket_no: str, dt: datetime):
    unit = Unit(name=f"Invoice Date Unit {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"P-INV-DATE-{ticket_no}",
        description=f"Invoice Date Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=dt,
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


def test_invoice_generate_renders_quick_range_buttons(client):
    response = client.get("/invoices/generate")

    assert response.status_code == 200
    assert 'data-quick-range="today"' in response.text
    assert 'data-quick-range="last-7-days"' in response.text
    assert 'data-quick-range="this-week"' in response.text
    assert 'data-quick-range="this-month"' in response.text
    assert 'data-quick-range="last-month"' in response.text


def test_invoice_generate_preview_accepts_single_uk_date(client, db_session):
    customer = Customer(account_code="C-INV-DATE-1", name="Invoice Date Customer 1")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-1",
        dt=datetime(2026, 2, 12, 10, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "12/02/2026",
            "date_to": "12/02/2026",
        },
    )

    assert response.status_code == 200
    assert "must be valid" not in response.text
    assert "is required." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_accepts_uk_date_range(client, db_session):
    customer = Customer(account_code="C-INV-DATE-2", name="Invoice Date Customer 2")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-2",
        dt=datetime(2026, 2, 15, 9, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." not in response.text
    assert "Date to must be valid (dd/mm/yyyy)." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_accepts_iso_date_range(client, db_session):
    customer = Customer(account_code="C-INV-DATE-ISO", name="Invoice Date Customer ISO")
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-INV-DATE-ISO",
        dt=datetime(2026, 2, 15, 9, 0, 0),
    )

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "2026-02-01",
            "date_to": "2026-02-28",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." not in response.text
    assert "Date to must be valid (dd/mm/yyyy)." not in response.text
    assert ticket.ticket_no in response.text


def test_invoice_generate_preview_rejects_invalid_uk_date(client, db_session):
    customer = Customer(account_code="C-INV-DATE-3", name="Invoice Date Customer 3")
    db_session.add(customer)
    db_session.commit()

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "31/02/2026",
            "date_to": "31/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be valid (dd/mm/yyyy)." in response.text
    assert "Date to must be valid (dd/mm/yyyy)." in response.text


def test_invoice_generate_preview_rejects_from_after_to(client, db_session):
    customer = Customer(account_code="C-INV-DATE-4", name="Invoice Date Customer 4")
    db_session.add(customer)
    db_session.commit()

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "28/02/2026",
            "date_to": "01/02/2026",
        },
    )

    assert response.status_code == 200
    assert "Date from must be on or before date to." in response.text
