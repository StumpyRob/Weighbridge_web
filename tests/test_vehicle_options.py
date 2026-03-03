from sqlalchemy import delete, func, select

from app.models import Customer, Driver, Vehicle, VehicleType
from app.seed import seed_vehicle_types


def test_vehicle_form_shows_all_customers_and_drivers(client, db_session):
    seed_vehicle_types(db_session)
    active_customer = Customer(account_code="C-ACT", name="Active Customer")
    stopped_customer = Customer(
        account_code="C-STOP", name="Dave Green", on_stop=True
    )
    driver_one = Driver(name="Alice Driver")
    driver_two = Driver(name="Bob Driver")
    db_session.add_all([active_customer, stopped_customer, driver_one, driver_two])
    db_session.commit()

    response = client.get("/vehicles/new")

    assert response.status_code == 200
    assert "Active Customer" in response.text
    assert "Dave Green" not in response.text
    assert "Alice Driver" in response.text
    assert "Bob Driver" in response.text


def test_vehicle_form_shows_seeded_vehicle_types(client, db_session):
    seed_vehicle_types(db_session)

    response = client.get("/vehicles/new")

    assert response.status_code == 200
    assert "Rigid" in response.text


def test_vehicle_form_auto_seeds_vehicle_types_when_empty(client, db_session):
    db_session.execute(delete(VehicleType))
    db_session.commit()

    response = client.get("/vehicles/new")

    assert response.status_code == 503
    assert (
        "System not initialized: missing required lookups (vehicle types)." in response.text
    )
    assert db_session.execute(select(func.count(VehicleType.id))).scalar_one() == 0


def test_vehicle_create_and_edit_persists_default_driver(client, db_session):
    driver_one = Driver(name="Default Driver One")
    driver_two = Driver(name="Default Driver Two")
    db_session.add_all([driver_one, driver_two])
    db_session.commit()

    create_response = client.post(
        "/vehicles/new",
        data={
            "registration": "DFT123",
            "default_driver_id": str(driver_one.id),
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303

    vehicle = db_session.execute(
        select(Vehicle).where(Vehicle.registration == "DFT123")
    ).scalar_one()
    assert vehicle.default_driver_id == driver_one.id

    edit_response = client.post(
        f"/vehicles/{vehicle.id}",
        data={
            "registration": "DFT123",
            "default_driver_id": str(driver_two.id),
        },
        follow_redirects=False,
    )
    assert edit_response.status_code == 303

    db_session.refresh(vehicle)
    assert vehicle.default_driver_id == driver_two.id
