from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    Driver,
    EwcCode,
    Haulier,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Vehicle,
)


def test_vehicle_suggestion_does_not_auto_apply(client, db_session):
    customer_a = Customer(account_code="C-A", name="Customer A")
    customer_b = Customer(account_code="C-B", name="Customer B")
    vehicle = Vehicle(registration="CO04DVE")
    ticket = Ticket(
        ticket_no="T-SUGGEST-1",
        datetime=datetime(2026, 1, 4, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer_a.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer_a, customer_b])
    db_session.commit()
    vehicle.default_customer_id = customer_b.id
    db_session.add(vehicle)
    db_session.commit()
    ticket.customer_id = customer_a.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(
        "/tickets/vehicle-suggest",
        params={
            "reg": "CO04DVE",
            "ticket_id": ticket.id,
            "direction": "INWARD",
            "customer_id": str(customer_a.id),
            "haulier_id": "",
            "driver_id": "",
            "gross_kg": "",
            "tare_kg": "",
            "readout_kg": "",
        },
    )

    assert response.status_code == 200
    assert "Known vehicle:" in response.text
    assert "CO04DVE" in response.text
    assert "Suggested customer:" in response.text
    assert "Customer B" in response.text
    assert 'hx-post="/tickets/vehicle-suggestion/apply"' in response.text
    assert 'hx-post="/tickets/vehicle-suggestion/dismiss"' in response.text
    assert "apply-default-customer" not in response.text
    assert "apply-default-haulier" not in response.text
    assert "apply-defaults" not in response.text
    assert 'hx-swap-oob="true"' not in response.text
    assert 'hx-swap-oob="innerHTML"' not in response.text
    assert f'value="{customer_b.id}" selected' not in response.text
    db_session.refresh(ticket)
    assert ticket.customer_id == customer_a.id


def test_vehicle_suggestion_apply_updates_fields(client, db_session):
    customer_a = Customer(account_code="C-A2", name="Customer A2")
    customer_b = Customer(account_code="C-B2", name="Customer B2")
    haulier = Haulier(name="Applied Haulier")
    driver = Driver(name="Applied Driver")
    vehicle = Vehicle(registration="CO04DVE")
    ticket = Ticket(
        ticket_no="T-SUGGEST-2",
        datetime=datetime(2026, 1, 4, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=None,
        gross_kg=12000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer_a, customer_b, haulier, driver])
    db_session.commit()
    vehicle.default_customer_id = customer_b.id
    vehicle.default_haulier_id = haulier.id
    vehicle.default_driver_id = driver.id
    vehicle.default_tare_kg = 5000
    db_session.add(vehicle)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/vehicle-suggestion/apply",
        data={"ticket_id": str(ticket.id), "reg": "CO04DVE"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Applied vehicle suggestion." not in response.text
    assert 'id="customer_id" name="customer_id" hx-swap-oob="true"' in response.text
    assert f'value="{customer_b.id}" selected' in response.text
    assert 'id="haulier_id" name="haulier_id" hx-swap-oob="true"' in response.text
    assert f'value="{haulier.id}" selected' in response.text
    assert 'id="driver_id" name="driver_id" hx-swap-oob="true"' in response.text
    assert f'value="{driver.id}" selected' in response.text
    assert 'id="weights-block" hx-swap-oob="innerHTML"' in response.text
    assert "Tare applied from vehicle default." in response.text

    db_session.refresh(ticket)
    assert ticket.vehicle_id == vehicle.id
    assert ticket.customer_id == customer_b.id
    assert ticket.haulier_id == haulier.id
    assert ticket.driver_id == driver.id
    assert float(ticket.tare_kg) == 5000.0
    assert float(ticket.net_kg) == 7000.0


def test_vehicle_suggestion_does_not_render_panel_when_already_applied(client, db_session):
    customer = Customer(account_code="C-SAME", name="Customer Same")
    haulier = Haulier(name="Same Haulier")
    driver = Driver(name="Same Driver")
    vehicle = Vehicle(registration="CO11SAM")
    ticket = Ticket(
        ticket_no="T-SUGGEST-SAME-1",
        datetime=datetime(2026, 1, 4, 10, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        tare_kg=5000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, haulier, driver])
    db_session.commit()
    vehicle.default_customer_id = customer.id
    vehicle.default_haulier_id = haulier.id
    vehicle.default_driver_id = driver.id
    vehicle.default_tare_kg = 5000
    db_session.add(vehicle)
    db_session.commit()
    ticket.customer_id = customer.id
    ticket.haulier_id = haulier.id
    ticket.driver_id = driver.id
    ticket.vehicle_id = vehicle.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(
        "/tickets/vehicle-suggest",
        params={
            "reg": vehicle.registration,
            "ticket_id": ticket.id,
            "direction": "INWARD",
            "customer_id": str(customer.id),
            "haulier_id": str(haulier.id),
            "driver_id": str(driver.id),
            "gross_kg": "",
            "tare_kg": "5000",
            "readout_kg": "",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Known vehicle:" not in response.text
    assert "Suggested customer:" not in response.text
    assert "Applied vehicle suggestion." not in response.text
    assert 'hx-post="/tickets/vehicle-suggestion/apply"' not in response.text


def test_vehicle_suggestion_dismiss_returns_empty_fragment(client):
    response = client.post(
        "/tickets/vehicle-suggestion/dismiss",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.text == ""


def test_vehicle_suggestion_apply_allows_on_stop_customer_with_warning(client, db_session):
    customer_a = Customer(account_code="C-A3", name="Customer A3")
    customer_b = Customer(account_code="C-B3", name="Customer B3", on_stop=True)
    vehicle = Vehicle(registration="CO09DVE")
    ticket = Ticket(
        ticket_no="T-SUGGEST-3",
        datetime=datetime(2026, 1, 4, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer_a, customer_b])
    db_session.commit()
    vehicle.default_customer_id = customer_b.id
    db_session.add(vehicle)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/vehicle-suggestion/apply",
        data={"ticket_id": str(ticket.id), "reg": "CO09DVE"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "Applied vehicle suggestion." not in response.text
    assert (
        "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        in response.text
    )
    assert (
        response.text.count(
            "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
        )
        == 1
    )
    assert 'id="customer_id" name="customer_id" hx-swap-oob="true"' in response.text
    assert f'value="{customer_b.id}" selected' in response.text
    assert 'id="ticket-status-warnings" hx-swap-oob="innerHTML"' in response.text
    assert "cannot be applied" not in response.text

    db_session.refresh(ticket)
    assert ticket.customer_id == customer_b.id


def test_ticket_save_auto_default_on_stop_customer_shows_warning(client, db_session):
    on_stop_warning = "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
    customer_stop = Customer(
        account_code="C-SAVE-STOP-1",
        name="Save Stop Customer",
        on_stop=True,
    )
    vehicle = Vehicle(registration="CO77SAV", default_customer_id=customer_stop.id)
    ticket = Ticket(
        ticket_no="T-SAVE-STOP-1",
        datetime=datetime(2026, 1, 5, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(customer_stop)
    db_session.commit()
    vehicle.default_customer_id = customer_stop.id
    db_session.add(vehicle)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-01-05T09:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": "",
            "vehicle_id": str(vehicle.id),
            "reg": vehicle.registration,
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert on_stop_warning in response.text
    assert "alert--warning" in response.text
    db_session.refresh(ticket)
    assert ticket.customer_id == customer_stop.id


def test_on_stop_customer_can_be_applied_via_all_paths_but_cannot_complete(client, db_session):
    on_stop_warning = (
        "Customer is ON STOP - allowed to record ticket; cannot complete/invoice."
    )
    completion_block = "Cannot complete ticket: Customer is ON STOP."

    customer_open = Customer(account_code="C-STOP-PATH-OPEN", name="Path Open Customer")
    customer_stop = Customer(
        account_code="C-STOP-PATH-LOCK",
        name="Path Stop Customer",
        on_stop=True,
    )
    haulier = Haulier(name="Path Haulier", carrier_licence_number="CBDU12345")
    vehicle = Vehicle(registration="CO55STP")
    destination = Destination(name="Path Destination")
    ewc = EwcCode(
        code_6="050505",
        code_display="05 05 05",
        description="Path EWC",
        hazardous=False,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    product = Product(
        code="P-STOP-PATH",
        description="Path Product",
        unit_price=Decimal("5.00"),
        ewc_code=ewc,
    )
    db_session.add_all(
        [customer_open, customer_stop, haulier, vehicle, destination, ewc, product]
    )
    db_session.commit()
    vehicle.default_customer_id = customer_stop.id
    vehicle.default_haulier_id = haulier.id
    db_session.add(vehicle)
    db_session.commit()

    def create_ticket(ticket_no: str, customer_id: int | None) -> Ticket:
        ticket = Ticket(
            ticket_no=ticket_no,
            datetime=datetime(2026, 1, 4, 12, 0, 0),
            status=TicketStatusEnum.OPEN.value,
            direction=DirectionEnum.INWARD.value,
            transaction_type=TransactionTypeEnum.WASTEIN.value,
            customer_id=customer_id,
            dont_invoice=False,
            paid=False,
        )
        db_session.add(ticket)
        db_session.commit()
        return ticket

    def assert_complete_blocked(ticket: Ticket) -> None:
        db_session.refresh(ticket)
        response = client.post(
            f"/tickets/{ticket.id}",
            data={
                "action": "complete",
                "datetime": "2026-01-04T12:00",
                "direction": "INWARD",
                "transaction_type": "WASTEIN",
                "customer_id": str(ticket.customer_id or ""),
                "haulier_id": str(ticket.haulier_id or ""),
                "product_id": str(product.id),
                "destination_id": str(destination.id),
                "vehicle_id": str(vehicle.id),
                "gross_kg": "12000",
                "tare_kg": "4000",
                "reg": vehicle.registration,
            },
        )
        assert response.status_code == 400
        assert completion_block in response.text
        db_session.refresh(ticket)
        status = ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
        assert status == TicketStatusEnum.OPEN.value

    ticket_unified = create_ticket("T-STOP-PATH-1", None)
    response = client.post(
        "/tickets/vehicle-suggestion/apply",
        data={"ticket_id": str(ticket_unified.id), "reg": vehicle.registration},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert on_stop_warning in response.text
    db_session.refresh(ticket_unified)
    assert ticket_unified.customer_id == customer_stop.id
    assert_complete_blocked(ticket_unified)

    ticket_default_customer = create_ticket("T-STOP-PATH-2", None)
    response = client.post(
        f"/tickets/{ticket_default_customer.id}/apply-default-customer",
        data={"reg": vehicle.registration},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert on_stop_warning in response.text
    db_session.refresh(ticket_default_customer)
    assert ticket_default_customer.customer_id == customer_stop.id
    assert_complete_blocked(ticket_default_customer)

    ticket_defaults = create_ticket("T-STOP-PATH-3", None)
    response = client.post(
        f"/tickets/{ticket_defaults.id}/apply-defaults",
        data={"reg": vehicle.registration},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert on_stop_warning in response.text
    db_session.refresh(ticket_defaults)
    assert ticket_defaults.customer_id == customer_stop.id
    assert_complete_blocked(ticket_defaults)

    ticket_save = create_ticket("T-STOP-PATH-4", None)
    response = client.post(
        f"/tickets/{ticket_save.id}",
        data={
            "action": "save",
            "datetime": "2026-01-04T12:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": "",
            "vehicle_id": str(vehicle.id),
            "reg": vehicle.registration,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(ticket_save)
    assert ticket_save.customer_id == customer_stop.id
    view_response = client.get(f"/tickets/{ticket_save.id}")
    assert view_response.status_code == 200
    assert "allowed to record ticket; cannot complete/invoice." in view_response.text
    assert_complete_blocked(ticket_save)


def test_ticket_save_autofills_default_driver_when_empty(client, db_session):
    driver = Driver(name="Vehicle Default Driver")
    vehicle = Vehicle(registration="DR11DEF")
    ticket = Ticket(
        ticket_no="T-DRIVER-DEFAULT-1",
        datetime=datetime(2026, 1, 6, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([driver, vehicle, ticket])
    db_session.commit()
    vehicle.default_driver_id = driver.id
    db_session.add(vehicle)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-01-06T09:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "vehicle_id": str(vehicle.id),
            "reg": vehicle.registration,
            "driver_id": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.driver_id == driver.id


def test_ticket_save_does_not_overwrite_existing_driver_with_vehicle_default(client, db_session):
    existing_driver = Driver(name="Already Set Driver")
    default_driver = Driver(name="Vehicle Default Driver 2")
    vehicle = Vehicle(registration="DR22DEF", default_driver_id=default_driver.id)
    ticket = Ticket(
        ticket_no="T-DRIVER-DEFAULT-2",
        datetime=datetime(2026, 1, 6, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        driver_id=existing_driver.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([existing_driver, default_driver])
    db_session.commit()
    vehicle.default_driver_id = default_driver.id
    db_session.add(vehicle)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-01-06T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "vehicle_id": str(vehicle.id),
            "reg": vehicle.registration,
            "driver_id": str(existing_driver.id),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.driver_id == existing_driver.id
