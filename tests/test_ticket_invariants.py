from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    EwcCode,
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
    customer = Customer(account_code="C-WALK", name="Walk-in Customer")
    destination = Destination(name="Walk-in Destination")
    ewc = EwcCode(
        code_6="010101",
        code_display="01 01 01",
        description="Test EWC",
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    product = Product(
        code="P-WALK",
        description="Walk-in",
        unit_price=Decimal("5.00"),
        ewc_code=ewc,
    )
    ticket = Ticket(
        ticket_no="T-WALK-1",
        datetime=datetime(2026, 1, 2, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, ewc, product, ticket])
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
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "destination_id": str(destination.id),
            "walk_in": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.walk_in is True


def test_complete_allows_reg_text_without_vehicle(client, db_session):
    customer = Customer(account_code="C-REG", name="Reg Customer")
    destination = Destination(name="Reg Destination")
    ewc = EwcCode(
        code_6="020202",
        code_display="02 02 02",
        description="Test EWC 2",
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    product = Product(
        code="P-REG",
        description="Reg ticket",
        unit_price=Decimal("3.50"),
        ewc_code=ewc,
    )
    ticket = Ticket(
        ticket_no="T-REG-1",
        datetime=datetime(2026, 1, 2, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, ewc, product, ticket])
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
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "destination_id": str(destination.id),
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


def test_complete_blocks_on_stop_customer(client, db_session):
    customer = Customer(account_code="C-STOP", name="Stop Customer", on_stop=True)
    destination = Destination(name="Stop Destination")
    ewc = EwcCode(
        code_6="030303",
        code_display="03 03 03",
        description="Stop EWC",
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    product = Product(
        code="P-STOP",
        description="Stop product",
        unit_price=Decimal("4.00"),
        ewc_code=ewc,
    )
    ticket = Ticket(
        ticket_no="T-STOP-1",
        datetime=datetime(2026, 1, 3, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-01-03T09:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "gross_kg": "2200",
            "tare_kg": "1200",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "destination_id": str(destination.id),
            "reg": "ZZ99ZZZ",
        },
    )

    assert response.status_code == 400
    assert "Customer is ON STOP" in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_complete_defaults_tare_from_vehicle_default(client, db_session):
    customer = Customer(account_code="C-DEF", name="Default Customer")
    destination = Destination(name="Default Destination")
    vehicle = Vehicle(registration="ABC123", default_tare_kg=5000)
    ewc = EwcCode(
        code_6="040404",
        code_display="04 04 04",
        description="Default tare EWC",
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    product = Product(
        code="P-DEF",
        description="Default tare product",
        unit_price=Decimal("6.00"),
        ewc_code=ewc,
    )
    ticket = Ticket(
        ticket_no="T-DEF-1",
        datetime=datetime(2026, 1, 3, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, vehicle, ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-01-03T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "gross_kg": "10000",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "destination_id": str(destination.id),
            "reg": "ABC123",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.vehicle_id == vehicle.id
    assert float(ticket.tare_kg) == 5000.0
