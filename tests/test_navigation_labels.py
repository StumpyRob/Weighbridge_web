from datetime import date, datetime
from decimal import Decimal

from app.models import Customer, DirectionEnum, Invoice, Ticket, TicketStatusEnum, TransactionTypeEnum


def test_complete_ticket_shows_back_not_cancel(client, db_session):
    ticket = Ticket(
        ticket_no="T-NAV-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert '>Back</a>' in response.text
    assert '>Cancel</a>' not in response.text
    assert response.text.index('<summary class="frame-header">Void Ticket</summary>') < response.text.index('>Back</a>')


def test_open_ticket_shows_cancel(client, db_session):
    ticket = Ticket(
        ticket_no="T-NAV-2",
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
    assert '>Cancel</a>' in response.text


def test_paid_invoice_shows_back_to_invoices(client, db_session):
    customer = Customer(account_code="C-NAV-1", name="Nav Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-NAV-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 1),
        status="PAID",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert 'class="btn btn--secondary" href="/invoices">Back' in response.text
