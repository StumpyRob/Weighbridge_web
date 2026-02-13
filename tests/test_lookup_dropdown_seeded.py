from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    VoidReason,
)
from app.seed import seed_payment_methods, seed_void_reasons


def test_ticket_void_reason_dropdown_has_seeded_options(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-SEED-VOID-1",
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
    assert "Customer cancelled" in response.text
    assert "Other" in response.text


def test_ticket_void_reason_dropdown_auto_seeds_on_empty_table(client, db_session):
    db_session.execute(delete(VoidReason))
    db_session.commit()
    ticket = Ticket(
        ticket_no="T-SEED-VOID-AUTO-1",
        datetime=datetime(2026, 1, 1, 10, 5, 0),
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
    assert "No void reasons configured" not in response.text
    assert "Entered in error" in response.text
    assert "Duplicate ticket" in response.text
    assert "Customer cancelled" in response.text
    active_codes = {
        row.code
        for row in db_session.execute(
            select(VoidReason).where(VoidReason.is_active.is_(True))
        ).scalars()
    }
    assert {"Entered in error", "Duplicate ticket", "Customer cancelled"} <= active_codes


def test_seed_void_reasons_is_idempotent_and_reactivates_existing_code(db_session):
    db_session.add(
        VoidReason(
            code="Duplicate ticket",
            description="Legacy duplicate",
            is_active=False,
        )
    )
    db_session.commit()

    seed_void_reasons(db_session)
    created_second = seed_void_reasons(db_session)

    rows = db_session.execute(
        select(VoidReason).where(func.lower(VoidReason.code) == "duplicate ticket")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_active is True
    assert rows[0].description == "Duplicate ticket"
    assert created_second == 0


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
