import re
from datetime import date, datetime
from decimal import Decimal

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)


def _make_customer(
    db_session,
    *,
    account_code: str,
    name: str,
    credit_limit_pence: int | None,
) -> Customer:
    customer = Customer(
        account_code=account_code,
        name=name,
        credit_limit_pence=credit_limit_pence,
    )
    db_session.add(customer)
    db_session.commit()
    return customer


def _make_ticket(
    db_session,
    *,
    customer_id: int,
    ticket_no: str,
    qty: float | None = None,
    unit_price: Decimal | None = None,
    total: Decimal | None = None,
) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 2, 20, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer_id,
        qty=qty,
        unit_price=unit_price,
        total=total,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def _add_invoice(
    db_session,
    *,
    customer_id: int,
    invoice_no: str,
    status: str,
    gross_total: Decimal,
) -> Invoice:
    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer_id,
        invoice_date=date(2026, 2, 20),
        status=status,
        net_total=gross_total,
        vat_total=Decimal("0.00"),
        gross_total=gross_total,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_ticket_credit_banner_hidden_when_customer_has_no_credit_limit(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-CR-NOLIMIT-1",
        name="No Credit Limit Customer",
        credit_limit_pence=None,
    )
    ticket = _make_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-CR-NOLIMIT-1",
        total=Decimal("25.00"),
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Approaching credit limit" not in response.text
    assert "Over credit limit" not in response.text
    assert "ticket-credit-banner" not in response.text


def test_ticket_credit_banner_shows_approaching_at_or_above_80_percent(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-CR-APPROACH-1",
        name="Approaching Credit Customer",
        credit_limit_pence=10000,
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-APP-OPEN-1",
        status="OPEN",
        gross_total=Decimal("60.00"),
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-APP-DRAFT-1",
        status="DRAFT",
        gross_total=Decimal("50.00"),
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-APP-PAID-1",
        status="PAID",
        gross_total=Decimal("75.00"),
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-APP-VOID-1",
        status="VOID",
        gross_total=Decimal("25.00"),
    )
    ticket = _make_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-CR-APPROACH-1",
        qty=2,
        unit_price=Decimal("10.00"),
        total=None,
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Approaching credit limit" in response.text
    assert "Over credit limit" not in response.text
    assert 'id="ticket-credit-limit-banner-help"' in response.text
    assert 'id="ticket-credit-limit-limit-help"' in response.text
    assert 'id="ticket-credit-limit-outstanding-help"' in response.text
    assert 'id="ticket-credit-limit-this-ticket-help"' in response.text
    assert 'id="ticket-credit-limit-projected-help"' in response.text
    assert "&pound;60.00" in response.text
    assert "&pound;80.00" in response.text


def test_ticket_credit_banner_shows_over_when_projected_exceeds_limit(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-CR-OVER-1",
        name="Over Credit Customer",
        credit_limit_pence=10000,
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-OVER-OPEN-1",
        status="OPEN",
        gross_total=Decimal("95.00"),
    )
    ticket = _make_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-CR-OVER-1",
        total=Decimal("10.00"),
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Over credit limit" in response.text
    assert "Approaching credit limit" not in response.text
    assert "&pound;105.00" in response.text


def test_ticket_credit_banner_shows_dash_when_ticket_total_cannot_be_estimated(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-CR-UNKNOWN-1",
        name="Unknown Ticket Value Customer",
        credit_limit_pence=10000,
    )
    _add_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-CR-UNKNOWN-OPEN-1",
        status="OPEN",
        gross_total=Decimal("85.00"),
    )
    ticket = _make_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-CR-UNKNOWN-1",
        qty=None,
        unit_price=None,
        total=None,
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Approaching credit limit" in response.text
    assert (
        re.search(
            r'ticket-credit-limit-this-ticket-help"[\s\S]*?&mdash;',
            response.text,
        )
        is not None
    )
    assert "&pound;85.00" in response.text


def test_customer_credit_limit_label_no_longer_shows_wip_badge(client):
    response = client.get("/customers/new")

    assert response.status_code == 200
    assert 'Credit limit <span class="badge badge--muted"' not in response.text
    assert 'id="credit_limit_pounds"' in response.text
