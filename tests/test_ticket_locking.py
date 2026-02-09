from datetime import datetime

from app.models import DirectionEnum, Ticket, TicketStatusEnum, TransactionTypeEnum


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
