from decimal import Decimal

from app.models import Customer, Product, Vehicle


def test_customers_list_shows_saved_banner(client, db_session):
    customer = Customer(account_code="C-SAVED-1", name="Saved Customer")
    db_session.add(customer)
    db_session.commit()

    response = client.get("/customers?saved=1")

    assert response.status_code == 200
    assert "Saved." in response.text


def test_vehicles_list_shows_saved_banner(client, db_session):
    vehicle = Vehicle(registration="SAVE123")
    db_session.add(vehicle)
    db_session.commit()

    response = client.get("/vehicles?saved=1")

    assert response.status_code == 200
    assert "Saved." in response.text


def test_products_list_shows_saved_banner(client, db_session):
    product = Product(
        code="P-SAVED-1",
        description="Saved Product",
        unit_price=Decimal("1.00"),
    )
    db_session.add(product)
    db_session.commit()

    response = client.get("/products?saved=1")

    assert response.status_code == 200
    assert "Saved." in response.text


def test_customers_list_hides_saved_banner_without_flag(client, db_session):
    customer = Customer(account_code="C-SAVED-2", name="Saved Customer 2")
    db_session.add(customer)
    db_session.commit()

    response = client.get("/customers")

    assert response.status_code == 200
    assert "Saved." not in response.text
