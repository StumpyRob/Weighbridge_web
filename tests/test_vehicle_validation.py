from sqlalchemy import select

from app.models import Container, Vehicle, VehicleTare


def test_vehicle_create_rejects_negative_default_tare(client, db_session):
    response = client.post(
        "/vehicles/new",
        data={
            "registration": "NEG001",
            "default_tare_kg": "-9999",
        },
    )

    assert response.status_code == 400
    assert "Default tare must be 0 or greater." in response.text
    assert (
        db_session.execute(select(Vehicle).where(Vehicle.registration == "NEG001")).first()
        is None
    )


def test_vehicle_update_rejects_negative_overweight_threshold(client, db_session):
    vehicle = Vehicle(registration="NEG002")
    db_session.add(vehicle)
    db_session.commit()

    response = client.post(
        f"/vehicles/{vehicle.id}",
        data={
            "registration": "NEG002",
            "overweight_threshold_kg": "-1",
        },
    )

    assert response.status_code == 400
    assert "Overweight threshold must be 0 or greater." in response.text

    db_session.refresh(vehicle)
    assert vehicle.overweight_threshold_kg is None


def test_vehicle_tare_add_rejects_negative_values(client, db_session):
    vehicle = Vehicle(registration="NEG003")
    container = Container(name="Hook Bin", is_active=True)
    db_session.add_all([vehicle, container])
    db_session.commit()

    response = client.post(
        f"/vehicles/{vehicle.id}/tares",
        data={
            "container_id": str(container.id),
            "tare_kg": "-250",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/vehicles/{vehicle.id}?error=tare_negative"
    assert (
        db_session.execute(
            select(VehicleTare).where(VehicleTare.vehicle_id == vehicle.id)
        ).first()
        is None
    )


def test_vehicle_edit_shows_query_error_message_for_invalid_tare(client, db_session):
    vehicle = Vehicle(registration="NEG004")
    db_session.add(vehicle)
    db_session.commit()

    response = client.get(f"/vehicles/{vehicle.id}?error=tare_negative")

    assert response.status_code == 200
    assert "Tare must be 0 or greater." in response.text
