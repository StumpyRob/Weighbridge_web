from datetime import date, datetime
from decimal import Decimal

from app.models import Customer, DirectionEnum, Invoice, Ticket, TicketStatusEnum, TransactionTypeEnum
from app.seed import seed_payment_methods, seed_void_reasons


def test_ticket_void_reason_dropdown_has_seeded_options(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-SEED-VOID-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Customer cancelled" in response.text
    assert "Other" in response.text


def test_invoice_void_reason_dropdown_has_seeded_options(client, db_session):
    seed_void_reasons(db_session)
    customer = Customer(account_code="C-SEED-1", name="Seed Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-SEED-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 1),
        status="OPEN",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "Customer cancelled" in response.text
    assert "Other" in response.text


def test_invoice_payment_method_dropdown_has_seeded_options(client, db_session):
    seed_payment_methods(db_session)
    customer = Customer(account_code="C-SEED-2", name="Seed Customer 2")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-SEED-2",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 2),
        status="OPEN",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "Bank transfer" in response.text
