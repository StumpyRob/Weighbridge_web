from datetime import datetime

import pytest

from app.models import (
    Container,
    Destination,
    Driver,
    Haulier,
    Ticket,
    TicketStatusEnum,
    DirectionEnum,
    TransactionTypeEnum,
)


@pytest.fixture()
def lookup_ticket(db_session):
    haulier = Haulier(name="Test Haulier", is_active=True)
    driver = Driver(name="Test Driver", is_active=True)
    container = Container(name="Test Container", is_active=True)
    destination = Destination(name="Test Destination", is_active=True)
    db_session.add_all([haulier, driver, container, destination])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-LOOKUP-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
        haulier_id=haulier.id,
        driver_id=driver.id,
        container_id=container.id,
        destination_id=destination.id,
    )
    db_session.add(ticket)
    db_session.commit()

    return {
        "haulier": haulier,
        "driver": driver,
        "container": container,
        "destination": destination,
    }


@pytest.mark.parametrize(
    "key, path",
    [
        ("haulier", "/lookups/hauliers"),
        ("driver", "/lookups/drivers"),
        ("container", "/lookups/containers"),
        ("destination", "/lookups/destinations"),
    ],
)
def test_lookup_deactivate_guard(client, db_session, lookup_ticket, key, path):
    record = lookup_ticket[key]
    response = client.post(f"{path}/{record.id}/deactivate")

    assert response.status_code == 200
    assert "Cannot deactivate: in use by tickets." in response.text

    refreshed = db_session.get(type(record), record.id)
    assert refreshed is not None
    assert refreshed.is_active is True
