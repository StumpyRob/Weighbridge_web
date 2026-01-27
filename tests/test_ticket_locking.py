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

    assert response.status_code == 403
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert _status_value(ticket.direction) == DirectionEnum.INWARD.value
