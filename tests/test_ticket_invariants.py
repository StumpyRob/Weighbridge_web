from datetime import datetime
from decimal import Decimal

from app.models import (
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Vehicle,
)


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def test_weigh_out_requires_weigh_in(client, db_session):
    ticket = Ticket(
        ticket_no="T-ORDER-1",
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
        data={
            "action": "save",
            "datetime": "2026-01-01T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "tare_kg": "1200",
        },
    )

    assert response.status_code == 400
    assert "Weigh-in (gross) is required before tare." in response.text
    db_session.refresh(ticket)
    assert ticket.tare_kg is None


def test_complete_blocks_negative_net(client, db_session):
    vehicle = Vehicle(registration="ABC123")
    product = Product(code="P001", description="Test product", unit_price=Decimal("10.00"))
    ticket = Ticket(
        ticket_no="T-NET-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([vehicle, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-01-01T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "gross_kg": "1000",
            "tare_kg": "1500",
            "vehicle_id": str(vehicle.id),
            "product_id": str(product.id),
        },
    )

    assert response.status_code == 400
    assert "Net weight cannot be negative" in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
