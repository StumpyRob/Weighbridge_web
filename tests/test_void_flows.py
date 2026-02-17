from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceVoid,
    Ticket,
    TicketStatusEnum,
    TicketVoid,
    TransactionTypeEnum,
    VoidReason,
)
from app.seed import (
    VOID_REASON_TYPE_INVOICE,
    VOID_REASON_TYPE_TICKET,
    seed_invoice_void_reasons,
    seed_void_reasons,
)


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def test_ticket_void_post_with_reason_succeeds(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
        )
        .order_by(VoidReason.id)
        .limit(1)
    ).scalar_one()
    ticket = Ticket(
        ticket_no="T-VOID-OK-1",
        datetime=datetime(2026, 1, 5, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?voided=1")
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.VOID.value
    ticket_void = db_session.execute(
        select(TicketVoid).where(TicketVoid.ticket_id == ticket.id)
    ).scalar_one()
    assert ticket_void.reason_id == reason.id
    assert ticket_void.note == "No note provided."
    follow = client.get(response.headers["location"])
    assert follow.status_code == 200
    assert "Ticket voided." in follow.text


def test_ticket_void_post_without_reason_fails(client, db_session):
    ticket = Ticket(
        ticket_no="T-VOID-FAIL-1",
        datetime=datetime(2026, 1, 5, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}", data={"action": "void"})

    assert response.status_code == 400
    assert "Void reason is required." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value


def test_invoice_void_post_with_reason_succeeds(client, db_session):
    seed_invoice_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
        .limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-VOID-INV-1", name="Void Invoice Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-VOID-OK-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 5),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_reason_id": str(reason.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/invoices/{invoice.id}?voided=1")
    db_session.refresh(invoice)
    assert invoice.status == "VOID"
    invoice_void = db_session.execute(
        select(InvoiceVoid).where(InvoiceVoid.invoice_id == invoice.id)
    ).scalar_one()
    assert invoice_void.reason_id == reason.id
    assert invoice_void.note == "No note provided."
    follow = client.get(response.headers["location"])
    assert follow.status_code == 200
    assert f"Invoice {invoice.invoice_no} voided." in follow.text
    assert "Void reason:" in follow.text
    assert (reason.description or reason.code) in follow.text
    assert "Voided:" in follow.text
    assert f'action="/invoices/{invoice.id}/paid"' not in follow.text
    assert f'action="/invoices/{invoice.id}/void"' not in follow.text


def test_invoice_void_post_without_reason_fails(client, db_session):
    customer = Customer(account_code="C-VOID-INV-2", name="Void Invoice Customer 2")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-VOID-FAIL-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 5),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(f"/invoices/{invoice.id}/void", data={})

    assert response.status_code == 400
    assert "Void reason is required." in response.text
    db_session.refresh(invoice)
    assert invoice.status == "DRAFT"


def test_invoice_void_post_fails_when_invoice_is_paid(client, db_session):
    seed_invoice_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
        .limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-VOID-INV-3", name="Void Invoice Customer 3")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-VOID-PAID-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 5),
        status="PAID",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_reason_id": str(reason.id)},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Cannot void a paid invoice." in response.text


def test_invoice_void_post_fails_when_invoice_is_not_draft(client, db_session):
    seed_invoice_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
        .limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-VOID-INV-OPEN-1", name="Void Invoice Customer OPEN")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-VOID-OPEN-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 5),
        status="OPEN",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_reason_id": str(reason.id)},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Only draft invoices can be voided." in response.text
    db_session.refresh(invoice)
    assert invoice.status == "OPEN"


def test_ticket_void_other_requires_note(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason).where(
            VoidReason.code == "Other",
            VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
        )
    ).scalar_one()
    ticket = Ticket(
        ticket_no="T-VOID-OTHER-1",
        datetime=datetime(2026, 1, 5, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id), "void_note": ""},
    )

    assert response.status_code == 400
    assert "Void note is required when reason is Other." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value


def test_invoice_void_other_allows_empty_note(client, db_session):
    seed_invoice_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason).where(
            VoidReason.code == "Duplicate invoice",
            VoidReason.reason_type == VOID_REASON_TYPE_INVOICE,
        )
    ).scalar_one()
    customer = Customer(account_code="C-VOID-INV-4", name="Void Invoice Customer 4")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-VOID-OTHER-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 5),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_reason_id": str(reason.id), "void_note": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/invoices/{invoice.id}?voided=1")
    db_session.refresh(invoice)
    assert invoice.status == "VOID"
    invoice_void = db_session.execute(
        select(InvoiceVoid).where(InvoiceVoid.invoice_id == invoice.id)
    ).scalar_one()
    assert invoice_void.reason_id == reason.id
    assert invoice_void.note == "No note provided."
