from app.models import Vehicle


def test_vehicle_create_duplicate_registration_returns_validation_error(client, db_session):
    db_session.add(Vehicle(registration="DUP123"))
    db_session.commit()

    response = client.post(
        "/vehicles/new",
        data={"registration": "DUP123"},
    )

    assert response.status_code == 400
    assert "Registration already exists." in response.text


def test_vehicle_update_duplicate_registration_returns_validation_error(client, db_session):
    first = Vehicle(registration="UPD111")
    second = Vehicle(registration="UPD222")
    db_session.add_all([first, second])
    db_session.commit()

    response = client.post(
        f"/vehicles/{second.id}",
        data={"registration": "UPD111"},
    )

    assert response.status_code == 400
    assert "Registration already exists." in response.text
