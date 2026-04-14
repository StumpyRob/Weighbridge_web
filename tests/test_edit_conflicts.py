import re
from decimal import Decimal

from sqlalchemy import select

from app.models import Customer, Product, TaxRate, Unit, Vehicle
from app.services.edit_conflicts import STALE_EDIT_MESSAGE


def _row_version(html: str) -> str:
    match = re.search(r'name="row_version" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_customer_update_rejects_stale_edit_submission(client, db_session):
    customer = Customer(account_code="C-CONFLICT-1", name="Conflict Customer")
    db_session.add(customer)
    db_session.commit()

    edit_page = client.get(f"/customers/{customer.id}")
    stale_token = _row_version(edit_page.text)

    first_save = client.post(
        f"/customers/{customer.id}",
        data={
            "row_version": stale_token,
            "account_code": "C-CONFLICT-1",
            "name": "First Save",
        },
        follow_redirects=False,
    )
    assert first_save.status_code == 303

    db_session.refresh(customer)
    assert customer.name == "First Save"

    stale_save = client.post(
        f"/customers/{customer.id}",
        data={
            "row_version": stale_token,
            "account_code": "C-CONFLICT-1",
            "name": "Second Save",
        },
    )
    assert stale_save.status_code == 409
    assert STALE_EDIT_MESSAGE in stale_save.text

    db_session.refresh(customer)
    assert customer.name == "First Save"


def test_vehicle_update_rejects_stale_edit_submission(client, db_session):
    vehicle = Vehicle(registration="CONFLICT1")
    db_session.add(vehicle)
    db_session.commit()

    edit_page = client.get(f"/vehicles/{vehicle.id}")
    stale_token = _row_version(edit_page.text)

    first_save = client.post(
        f"/vehicles/{vehicle.id}",
        data={
            "row_version": stale_token,
            "registration": "CONFLICT1",
            "default_tare_kg": "1200",
        },
        follow_redirects=False,
    )
    assert first_save.status_code == 303

    db_session.refresh(vehicle)
    assert float(vehicle.default_tare_kg) == 1200.0

    stale_save = client.post(
        f"/vehicles/{vehicle.id}",
        data={
            "row_version": stale_token,
            "registration": "CONFLICT1",
            "default_tare_kg": "2400",
        },
    )
    assert stale_save.status_code == 409
    assert STALE_EDIT_MESSAGE in stale_save.text

    db_session.refresh(vehicle)
    assert float(vehicle.default_tare_kg) == 1200.0


def test_product_update_rejects_stale_edit_submission(client, db_session):
    unit = Unit(name="Conflict Each", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="CONFLICT VAT",
        description="Conflict VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-CONFLICT-1",
        description="Conflict Product",
        product_type="sale",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([unit, tax_rate, product])
    db_session.commit()

    edit_page = client.get(f"/products/{product.id}")
    stale_token = _row_version(edit_page.text)

    first_save = client.post(
        f"/products/{product.id}",
        data={
            "row_version": stale_token,
            "code": "P-CONFLICT-1",
            "description": "First Save",
            "sale_type": "COUNT",
            "product_type": "sale",
            "unit_id": str(unit.id),
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "10.00",
        },
        follow_redirects=False,
    )
    assert first_save.status_code == 303

    db_session.refresh(product)
    assert product.description == "First Save"

    stale_save = client.post(
        f"/products/{product.id}",
        data={
            "row_version": stale_token,
            "code": "P-CONFLICT-1",
            "description": "Second Save",
            "sale_type": "COUNT",
            "product_type": "sale",
            "unit_id": str(unit.id),
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "10.00",
        },
    )
    assert stale_save.status_code == 409
    assert STALE_EDIT_MESSAGE in stale_save.text

    db_session.refresh(product)
    assert product.description == "First Save"


def test_product_validation_error_retains_selected_inactive_unit(client, db_session):
    unit_current = Unit(name="Current Each", unit_type="COUNT", is_active=True)
    unit_selected = Unit(name="Selected Each", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="INACTIVE VAT",
        description="Inactive VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-INACTIVE-UNIT",
        description="Inactive Unit Product",
        product_type="sale",
        unit=unit_current,
        tax_rate=tax_rate,
        unit_price=Decimal("5.00"),
    )
    db_session.add_all([unit_current, unit_selected, tax_rate, product])
    db_session.commit()

    unit_selected.is_active = False
    db_session.commit()

    response = client.post(
        f"/products/{product.id}",
        data={
            "code": "",
            "description": "Inactive Unit Product",
            "sale_type": "COUNT",
            "product_type": "sale",
            "unit_id": str(unit_selected.id),
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "5.00",
        },
    )

    assert response.status_code == 400
    assert "Code is required." in response.text
    assert f'<option value="{unit_selected.id}" selected>Selected Each (inactive)</option>' in response.text
