from sqlalchemy import select

from app.models import Customer


def test_customer_create_preserves_postcode_spacing(client, db_session):
    response = client.post(
        "/customers/new",
        data={
            "account_code": "POST001",
            "name": "Postcode Customer",
            "postcode": "te1  1st",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/customers?saved=1"

    customer = db_session.execute(
        select(Customer).where(Customer.account_code == "POST001")
    ).scalar_one()
    assert customer.postcode == "TE1 1ST"


def test_customer_update_preserves_postcode_spacing(client, db_session):
    customer = Customer(account_code="POST002", name="Postcode Update")
    db_session.add(customer)
    db_session.commit()

    response = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": "POST002",
            "name": "Postcode Update",
            "postcode": "ab1   2cd",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/customers?saved=1"

    db_session.refresh(customer)
    assert customer.postcode == "AB1 2CD"
