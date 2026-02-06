from datetime import datetime

from app.models import (
    Customer,
    DirectionEnum,
    Haulier,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Vehicle,
)


def test_vehicle_suggest_applies_defaults_and_tare(client, db_session):
    customer = Customer(account_code="C-DEF", name="Default Customer")
    haulier = Haulier(name="Default Haulier")
    vehicle = Vehicle(
        registration="ABC123",
        default_customer_id=customer.id,
        default_haulier_id=haulier.id,
        default_tare_kg=5000,
    )
    ticket = Ticket(
        ticket_no="T-SUGGEST-1",
        datetime=datetime(2026, 1, 4, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, haulier])
    db_session.commit()
    vehicle.default_customer_id = customer.id
    vehicle.default_haulier_id = haulier.id
    db_session.add_all([vehicle, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/vehicle-suggest",
        params={
            "reg": "ABC123",
            "ticket_id": ticket.id,
            "direction": "INWARD",
            "customer_id": "",
            "haulier_id": "",
            "gross_kg": "12000",
            "tare_kg": "",
            "readout_kg": "",
        },
    )

    assert response.status_code == 200
    assert 'id="customer_id" name="customer_id" hx-swap-oob="true"' in response.text
    assert f'value="{customer.id}" selected' in response.text
    assert 'id="haulier_id" name="haulier_id" hx-swap-oob="true"' in response.text
    assert f'value="{haulier.id}" selected' in response.text
    assert 'id="weights-block" hx-swap-oob="innerHTML"' in response.text
    assert 'id="tare_kg"' in response.text
    assert 'value="5000"' in response.text
    assert 'value="7000"' in response.text
    assert "Tare auto-filled from vehicle default." in response.text


def test_vehicle_suggest_does_not_auto_apply_on_vehicle_mismatch(client, db_session):
    customer = Customer(account_code="C-A", name="Customer A")
    vehicle_a = Vehicle(registration="AAA111")
    vehicle_b = Vehicle(registration="BBB222", default_tare_kg=5000)
    ticket = Ticket(
        ticket_no="T-SUGGEST-2",
        datetime=datetime(2026, 1, 4, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        vehicle_id=vehicle_a.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer])
    db_session.commit()
    vehicle_a.owner_customer_id = customer.id
    db_session.add_all([vehicle_a, vehicle_b])
    db_session.commit()
    ticket.customer_id = customer.id
    ticket.vehicle_id = vehicle_a.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(
        "/tickets/vehicle-suggest",
        params={
            "reg": "BBB222",
            "ticket_id": ticket.id,
            "direction": "INWARD",
            "customer_id": "",
            "haulier_id": "",
            "gross_kg": "",
            "tare_kg": "",
            "readout_kg": "",
        },
    )

    assert response.status_code == 200
    assert "Ticket is linked to" in response.text
    assert 'hx-swap-oob="innerHTML"' not in response.text
