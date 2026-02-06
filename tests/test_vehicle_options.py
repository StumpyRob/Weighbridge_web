from app.models import Customer, Driver


def test_vehicle_form_shows_all_customers_and_drivers(client, db_session):
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
    assert "Dave Green (ON STOP)" in response.text
    assert "Alice Driver" in response.text
    assert "Bob Driver" in response.text
