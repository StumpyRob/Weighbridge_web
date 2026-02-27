from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    CustomerAdjustment,
    DirectionEnum,
    Invoice,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)
from app.services.credit import customer_outstanding_total


def _make_customer(
    db_session,
    *,
    account_code: str,
    name: str,
    credit_limit_pence: int = 10000,
) -> Customer:
    customer = Customer(
        account_code=account_code,
        name=name,
        credit_limit_pence=credit_limit_pence,
    )
    db_session.add(customer)
    db_session.commit()
    return customer


def _make_invoice(
    db_session,
    *,
    customer_id: int,
    invoice_no: str,
    gross_total: Decimal,
    status: str = "OPEN",
) -> Invoice:
    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer_id,
        invoice_date=date(2026, 2, 26),
        status=status,
        net_total=gross_total,
        vat_total=Decimal("0.00"),
        gross_total=gross_total,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def _make_ticket(
    db_session,
    *,
    customer_id: int,
    ticket_no: str,
    total: Decimal | None = None,
) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 2, 26, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer_id,
        total=total,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_customer_adjustment_reduces_outstanding_total(db_session):
    customer = _make_customer(
        db_session,
        account_code="C-ADJ-OUT-1",
        name="Adjustment Outstanding Customer",
    )
    _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-ADJ-OUT-1",
        gross_total=Decimal("50.00"),
        status="OPEN",
    )
    db_session.add_all(
        [
            CustomerAdjustment(
                customer_id=customer.id,
                amount_decimal=Decimal("-20.00"),
                reason="GOODWILL_CREDIT",
                note="Goodwill credit issued.",
                created_by_user_id=None,
            ),
            CustomerAdjustment(
                customer_id=customer.id,
                amount_decimal=Decimal("5.00"),
                reason="MANUAL_CORRECTION",
                note="Manual uplift.",
                created_by_user_id=None,
            ),
        ]
    )
    db_session.commit()

    outstanding = customer_outstanding_total(db_session, customer.id)

    assert outstanding == Decimal("35.00")


def test_credit_warning_banner_reflects_adjustment_immediately(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-ADJ-BAN-1",
        name="Adjustment Banner Customer",
    )
    _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-ADJ-BAN-1",
        gross_total=Decimal("85.00"),
        status="OPEN",
    )
    ticket = _make_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-ADJ-BAN-1",
        total=None,
    )

    before = client.get(f"/tickets/{ticket.id}")
    assert before.status_code == 200
    assert "Approaching credit limit" in before.text

    create_adjustment = client.post(
        f"/customers/{customer.id}/adjustments",
        data={
            "amount_decimal": "-10.00",
            "reason": "GOODWILL_CREDIT",
            "note": "Goodwill credit to release ticketing pressure.",
        },
        follow_redirects=False,
    )
    assert create_adjustment.status_code == 303
    assert create_adjustment.headers["location"].endswith(
        f"/customers/{customer.id}?saved=1&adjustment_saved=1"
    )

    after = client.get(f"/tickets/{ticket.id}")
    assert after.status_code == 200
    assert "Approaching credit limit" not in after.text
    assert "Over credit limit" not in after.text


def test_customer_adjustment_sets_audit_fields_on_create(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-ADJ-AUD-1",
        name="Adjustment Audit Customer",
    )

    response = client.post(
        f"/customers/{customer.id}/adjustments",
        data={
            "amount_decimal": "-12.50",
            "reason": "OTHER",
            "note": "Goodwill reconciliation credit approved.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(
        f"/customers/{customer.id}?saved=1&adjustment_saved=1"
    )
    adjustment = (
        db_session.execute(
            select(CustomerAdjustment)
            .where(CustomerAdjustment.customer_id == customer.id)
            .order_by(CustomerAdjustment.id.desc())
        )
        .scalars()
        .first()
    )
    assert adjustment is not None
    assert adjustment.amount_decimal == Decimal("-12.50")
    assert adjustment.reason == "OTHER"
    assert adjustment.note == "Goodwill reconciliation credit approved."
    assert adjustment.created_at is not None
    assert adjustment.created_by_user_id is None


def test_customer_adjustment_requires_note_for_audit_purposes(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-ADJ-NOTE-1",
        name="Adjustment Note Required Customer",
    )

    response = client.post(
        f"/customers/{customer.id}/adjustments",
        data={
            "amount_decimal": "-12.50",
            "reason": "OTHER",
            "note": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Note is required for audit purposes." in response.text
    assert 'account-adjustments-disclosure" open' in response.text
    adjustment = (
        db_session.execute(
            select(CustomerAdjustment).where(CustomerAdjustment.customer_id == customer.id)
        )
        .scalars()
        .first()
    )
    assert adjustment is None


def test_customer_adjustment_form_is_hidden_by_default(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-ADJ-UI-1",
        name="Adjustment UI Customer",
    )

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert "Account Adjustments" in response.text
    assert "+ Add adjustment" in response.text
    assert '<details class="account-adjustments-disclosure"' in response.text
    assert 'account-adjustments-disclosure" open' not in response.text
