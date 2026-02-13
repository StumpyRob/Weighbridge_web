from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Ticket,
    TicketStatusEnum,
    TicketVoid,
    TransactionTypeEnum,
    VoidReason,
)
from app.seed import seed_void_reasons


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def test_locked_ticket_update_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
        data={
            "action": "save",
            "datetime": "2026-01-01T10:00",
            "direction": "OUTWARD",
            "transaction_type": "WASTEIN",
        },
    )

    assert response.status_code == 400
    assert "This ticket is complete and cannot be edited." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert _status_value(ticket.direction) == DirectionEnum.INWARD.value


def test_void_ticket_update_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-2",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.VOID.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-01-01T10:00",
            "direction": "OUTWARD",
            "transaction_type": "WASTEIN",
        },
    )

    assert response.status_code == 400
    assert "This ticket is void and cannot be edited." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.VOID.value
    assert _status_value(ticket.direction) == DirectionEnum.INWARD.value


def test_complete_ticket_edit_screen_is_read_only(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-3",
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
    assert "This ticket is complete and cannot be edited." in response.text
    assert "<fieldset disabled" in response.text


def test_complete_ticket_locked_hides_save_complete_and_shows_void(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-LOCK-3A",
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
    assert 'name="action" value="save"' not in response.text
    assert 'name="action" value="complete"' not in response.text
    assert "<h2>Void Ticket</h2>" in response.text
    assert '<button type="submit" class="btn btn--danger">Void Ticket</button>' in response.text
    assert 'id="ticket-status-warnings"' not in response.text
    assert 'id="unsaved-changes-indicator"' not in response.text


def test_void_ticket_locked_hides_all_edit_actions(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-3B",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.VOID.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'name="action" value="save"' not in response.text
    assert 'name="action" value="complete"' not in response.text
    assert "<h2>Void Ticket</h2>" not in response.text
    assert "This ticket is void and cannot be edited." in response.text
    assert 'id="ticket-status-warnings"' not in response.text
    assert 'id="unsaved-changes-indicator"' not in response.text


def test_complete_ticket_void_post_allowed(client, db_session):
    db_session.execute(delete(VoidReason))
    db_session.commit()
    ticket = Ticket(
        ticket_no="T-LOCK-3C",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert "No void reasons configured" not in page.text
    assert "Entered in error" in page.text
    assert "Duplicate ticket" in page.text
    assert "Customer cancelled" in page.text
    reason = db_session.execute(
        select(VoidReason).where(VoidReason.is_active.is_(True)).limit(1)
    ).scalar_one()

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
    follow = client.get(response.headers["location"])
    assert follow.status_code == 200
    assert "Void reason:" in follow.text
    assert "Entered in error" in follow.text


def test_complete_ticket_void_without_reason_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-LOCK-3D",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
        data={"action": "void", "void_reason_id": ""},
    )

    assert response.status_code == 400
    assert "Void reason is required." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value


def test_open_ticket_void_post_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason).where(VoidReason.is_active.is_(True)).limit(1)
    ).scalar_one()
    ticket = Ticket(
        ticket_no="T-LOCK-3E",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
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
    )

    assert response.status_code == 400
    assert "Only complete tickets can be voided." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_invoiced_complete_ticket_void_post_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason).where(VoidReason.is_active.is_(True)).limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-LOCK-INV-1", name="Lock Invoice Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-LOCK-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 1),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-LOCK-3F",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        invoice_id=invoice.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
    )

    assert response.status_code == 400
    assert "Cannot void a ticket that has already been invoiced." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    ticket_void = db_session.execute(
        select(TicketVoid).where(TicketVoid.ticket_id == ticket.id)
    ).scalars().first()
    assert ticket_void is None


def test_complete_ticket_weights_render_display_only(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-4",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        gross_kg=12000,
        tare_kg=4000,
        net_kg=8000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert '<input type="number" step="1" id="gross_kg"' not in response.text
    assert '<input type="number" step="1" id="tare_kg"' not in response.text
    assert "Gross kg" in response.text
    assert "Tare kg" in response.text
    assert "Net kg" in response.text
    assert "12000" in response.text
    assert "4000" in response.text
    assert "8000" in response.text


def test_complete_ticket_weights_swap_preview_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-5",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        gross_kg=12000,
        tare_kg=4000,
        net_kg=8000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/weights/swap-preview",
        data={"ticket_id": str(ticket.id), "gross_kg": "12000", "tare_kg": "4000"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 400
    assert "This ticket is complete and cannot be edited." in response.text
