from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def test_invoice_generate_confirm_blocks_on_stop_customer(client, db_session):
    customer = Customer(account_code="C-INV-STOP-1", name="Invoice Stop", on_stop=True)
    db_session.add(customer)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-INV-STOP-1",
        datetime=datetime(2026, 1, 8, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    before_count = len(db_session.execute(select(Invoice)).scalars().all())

    response = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/01/2026",
            "date_to": "31/01/2026",
        },
    )

    assert response.status_code == 200
    assert "Cannot generate invoice: Customer is ON STOP." in response.text
    assert "cannot be completed/invoiced until stop is removed." in response.text

    after_count = len(db_session.execute(select(Invoice)).scalars().all())
    assert after_count == before_count

    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert ticket.invoice_id is None


def test_invoice_generate_confirm_ignores_open_tickets_for_stop_checks(client, db_session):
    customer = Customer(account_code="C-INV-MIX-1", name="Invoice Mixed", on_stop=False)
    unit = Unit(name="Invoiceable Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-INV-MIX-1",
        description="Invoiceable Product",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    invoiceable_ticket = Ticket(
        ticket_no="T-INV-MIX-1",
        datetime=datetime(2026, 1, 8, 10, 0, 0),
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
    open_ticket = Ticket(
        ticket_no="T-INV-MIX-2",
        datetime=datetime(2026, 1, 8, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([invoiceable_ticket, open_ticket])
    db_session.commit()

    before_count = len(db_session.execute(select(Invoice)).scalars().all())

    response = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/01/2026",
            "date_to": "31/01/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/invoices/")

    after_count = len(db_session.execute(select(Invoice)).scalars().all())
    assert after_count == before_count + 1

    db_session.refresh(invoiceable_ticket)
    db_session.refresh(open_ticket)
    assert invoiceable_ticket.invoice_id is not None
    assert _status_value(open_ticket.status) == TicketStatusEnum.OPEN.value
    assert open_ticket.invoice_id is None
