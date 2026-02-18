from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    PaymentMethod,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    VoidReason,
)
from app.seed import (
    VOID_REASON_TYPE_INVOICE,
    VOID_REASON_TYPE_TICKET,
    seed_invoice_void_reasons,
    seed_payment_methods,
    seed_void_reasons,
)


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
    assert "Duplicate invoice" not in response.text


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
    assert "Duplicate invoice" not in response.text
    active_codes = {
        row.code
        for row in db_session.execute(
            select(VoidReason).where(
                VoidReason.is_active.is_(True),
                VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
            )
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
    assert rows[0].reason_type == VOID_REASON_TYPE_TICKET
    assert created_second == 0


def test_seed_invoice_void_reasons_is_idempotent_and_reactivates_existing_code(
    db_session,
):
    db_session.add(
        VoidReason(
            code="Entered in error",
            reason_type=VOID_REASON_TYPE_INVOICE,
            description="Legacy invoice entered error",
            is_active=False,
        )
    )
    db_session.commit()

    seed_invoice_void_reasons(db_session)
    created_second = seed_invoice_void_reasons(db_session)

    rows = db_session.execute(
        select(VoidReason).where(
            func.lower(VoidReason.code) == "entered in error",
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_active is True
    assert rows[0].description == "Entered in error"
    assert created_second == 0


def test_invoice_void_reason_dropdown_has_seeded_options(client, db_session):
    seed_void_reasons(db_session)
    seed_invoice_void_reasons(db_session)
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
    assert "Entered in error" in response.text
    assert "Customer cancelled" in response.text
    assert "Duplicate invoice" in response.text
    assert "Duplicate ticket" not in response.text


def test_invoice_void_reason_dropdown_auto_seeds_on_empty_table(client, db_session):
    db_session.execute(delete(VoidReason))
    db_session.commit()
    customer = Customer(account_code="C-SEED-VOID-INV-AUTO", name="Seed Void Invoice Auto")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-SEED-VOID-INV-AUTO",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 4),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "No void reasons configured" not in response.text
    assert "Entered in error" in response.text
    assert "Customer cancelled" in response.text
    assert "Duplicate invoice" in response.text
    assert "Duplicate ticket" not in response.text
    active_codes = {
        row.code
        for row in db_session.execute(
            select(VoidReason).where(
                VoidReason.is_active.is_(True),
                VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
            )
        ).scalars()
    }
    assert {
        "Entered in error",
        "Customer cancelled",
        "Duplicate invoice",
    } <= active_codes


def test_void_reason_codes_can_overlap_between_ticket_and_invoice_types(db_session):
    seed_void_reasons(db_session)
    seed_invoice_void_reasons(db_session)

    entered_rows = db_session.execute(
        select(VoidReason).where(func.lower(VoidReason.code) == "entered in error")
    ).scalars().all()
    assert len(entered_rows) == 2
    assert {row.reason_type for row in entered_rows} == {
        VOID_REASON_TYPE_TICKET,
        VOID_REASON_TYPE_INVOICE,
    }


def test_void_reason_duplicate_code_is_blocked_within_same_type(db_session):
    db_session.add(
        VoidReason(
            code="Entered in error",
            reason_type=VOID_REASON_TYPE_INVOICE,
            description="Entered in error",
            is_active=True,
        )
    )
    db_session.commit()

    db_session.add(
        VoidReason(
            code="Entered in error",
            reason_type=VOID_REASON_TYPE_INVOICE,
            description="Duplicate",
            is_active=True,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_void_reason_unique_constraint_enforced_at_db_level(db_session):
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.execute(
        text(
            "INSERT INTO void_reasons "
            "(code, reason_type, description, is_active, created_at, updated_at) "
            "VALUES (:code, :reason_type, :description, :is_active, :created_at, :updated_at)"
        ),
        {
            "code": "Entered in error",
            "reason_type": VOID_REASON_TYPE_INVOICE,
            "description": "Entered in error",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    )
    db_session.commit()

    db_session.execute(
        text(
            "INSERT INTO void_reasons "
            "(code, reason_type, description, is_active, created_at, updated_at) "
            "VALUES (:code, :reason_type, :description, :is_active, :created_at, :updated_at)"
        ),
        {
            "code": "Entered in error",
            "reason_type": VOID_REASON_TYPE_TICKET,
            "description": "Entered in error",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        },
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO void_reasons "
                "(code, reason_type, description, is_active, created_at, updated_at) "
                "VALUES (:code, :reason_type, :description, :is_active, :created_at, :updated_at)"
            ),
            {
                "code": "Entered in error",
                "reason_type": VOID_REASON_TYPE_INVOICE,
                "description": "Duplicate",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
        )
        db_session.commit()
    db_session.rollback()


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
    assert "BACS" in response.text


def test_invoice_payment_method_dropdown_auto_seeds_on_empty_table(client, db_session):
    db_session.execute(delete(PaymentMethod))
    db_session.commit()
    customer = Customer(account_code="C-SEED-PM-AUTO", name="Seed Payment Auto")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-SEED-PM-AUTO",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 3),
        status="OPEN",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    for code in {"BACS", "Card", "Cash", "Cheque"}:
        assert code in response.text
    active_codes = {
        row.code
        for row in db_session.execute(
            select(PaymentMethod).where(PaymentMethod.is_active.is_(True))
        ).scalars()
    }
    assert {"BACS", "Card", "Cash", "Cheque"} <= active_codes


def test_seed_payment_methods_is_idempotent_and_reactivates_existing_code(db_session):
    db_session.add(
        PaymentMethod(
            code="Card",
            description="Legacy card",
            is_active=False,
        )
    )
    db_session.commit()

    seed_payment_methods(db_session)
    created_second = seed_payment_methods(db_session)

    rows = db_session.execute(
        select(PaymentMethod).where(func.lower(PaymentMethod.code) == "card")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_active is True
    assert rows[0].description == "Card"
    for code in {"bacs", "card", "cash", "cheque"}:
        code_rows = db_session.execute(
            select(PaymentMethod).where(func.lower(PaymentMethod.code) == code)
        ).scalars().all()
        assert len(code_rows) == 1
    assert created_second == 0

