from datetime import datetime

from app.models import DirectionEnum, Ticket, TicketStatusEnum, TransactionTypeEnum
from app.templating import templates


def test_ticket_edit_shows_readout_wip_hint_in_dev_mode(client, db_session):
    ticket = Ticket(
        ticket_no="T-WIP-READ-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = True
    try:
        response = client.get(f"/tickets/{ticket.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "Live weighbridge integration not wired yet." in response.text
    assert 'hx-post="/tickets/weights/read"' in response.text
    assert 'hx-target="#weights-block"' in response.text
    assert 'hx-swap="innerHTML"' in response.text
    assert "Read" in response.text
    assert "WIP" in response.text


def test_ticket_edit_hides_read_button_in_non_dev_mode(client, db_session):
    ticket = Ticket(
        ticket_no="T-READ-2",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = False
    try:
        response = client.get(f"/tickets/{ticket.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "Live weighbridge integration not wired yet." not in response.text
    assert 'hx-post="/tickets/weights/read"' not in response.text
    assert "WIP" not in response.text


def test_ticket_weights_read_endpoint_returns_friendly_not_implemented(client, db_session):
    ticket = Ticket(
        ticket_no="T-WIP-READ-3",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post("/tickets/weights/read", data={"ticket_id": str(ticket.id)})

    assert response.status_code == 400
    assert 'id="weights-form"' in response.text
    assert "Not implemented: live weighbridge readout integration is not wired yet." in response.text
