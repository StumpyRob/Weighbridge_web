from datetime import date, datetime
from decimal import Decimal

from app.models import Customer, DirectionEnum, Invoice, Ticket, TicketStatusEnum, TransactionTypeEnum
from app.templating import templates


def test_non_dev_pages_do_not_show_wip_text(client, db_session):
    ticket = Ticket(
        ticket_no="T-NONDEV-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    customer = Customer(account_code="C-NONDEV-1", name="Non Dev Customer")
    db_session.add_all([ticket, customer])
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-NONDEV-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 1),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = False
    try:
        ticket_response = client.get(f"/tickets/{ticket.id}")
        invoice_response = client.get(f"/invoices/{invoice.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert ticket_response.status_code == 200
    assert invoice_response.status_code == 200
    assert "WIP" not in ticket_response.text
    assert "WIP" not in invoice_response.text
