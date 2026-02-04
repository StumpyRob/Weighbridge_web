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


def test_complete_allows_walk_in_without_vehicle_or_reg(client, db_session):
    product = Product(code="P-WALK", description="Walk-in", unit_price=Decimal("5.00"))
    ticket = Ticket(
        ticket_no="T-WALK-1",
        datetime=datetime(2026, 1, 2, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-01-02T09:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "gross_kg": "2000",
            "tare_kg": "1200",
            "product_id": str(product.id),
            "walk_in": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.walk_in is True


def test_complete_allows_reg_text_without_vehicle(client, db_session):
    product = Product(code="P-REG", description="Reg ticket", unit_price=Decimal("3.50"))
    ticket = Ticket(
        ticket_no="T-REG-1",
        datetime=datetime(2026, 1, 2, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-01-02T10:00",
            "direction": "OUTWARD",
            "transaction_type": "WASTEOUT",
            "gross_kg": "1500",
            "tare_kg": "900",
            "product_id": str(product.id),
            "reg": "ab12 cde",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.vehicle_id is None
    assert ticket.vehicle_reg_text == "AB12CDE"


def test_invalid_complete_preserves_vehicle_reg_text(client, db_session):
    ticket = Ticket(
        ticket_no="T-REG-2",
        datetime=datetime(2026, 1, 2, 11, 0, 0),
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
            "action": "complete",
            "datetime": "2026-01-02T11:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "gross_kg": "1800",
            "tare_kg": "1200",
            "reg": "zz99zzz",
        },
    )

    assert response.status_code == 400
    assert 'value="ZZ99ZZZ"' in response.text
    db_session.refresh(ticket)
    assert ticket.vehicle_reg_text == "ZZ99ZZZ"
