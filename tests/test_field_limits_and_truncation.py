from datetime import datetime
from decimal import Decimal

from app.constants import DESC_MAX, NAME_MAX
from app.models import (
    Customer,
    DirectionEnum,
    Driver,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
)


def test_customer_create_rejects_overlong_name(client):
    response = client.post(
        "/customers/new",
        data={
            "account_code": "C-LIMIT-1",
            "name": "N" * (NAME_MAX + 1),
        },
    )

    assert response.status_code == 400
    assert f"Name must be {NAME_MAX} characters or fewer." in response.text


def test_product_create_rejects_overlong_description(client, db_session):
    tax_rate = TaxRate(
        code="LIMIT-VAT-20",
        description="Limit tax rate",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    response = client.post(
        "/products/new",
        data={
            "code": "P-LIMIT-1",
            "description": "D" * (DESC_MAX + 1),
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "1.00",
            "group_id": "",
            "nominal_code": "",
        },
    )

    assert response.status_code == 400
    assert f"Description must be {DESC_MAX} characters or fewer." in response.text


def test_customer_create_rejects_overlong_payment_terms(client):
    response = client.post(
        "/customers/new",
        data={
            "account_code": "C-LIMIT-TERMS-1",
            "name": "Payment Terms Length Test",
            "payment_terms": "T" * (NAME_MAX + 1),
        },
    )

    assert response.status_code == 400
    assert f"Payment terms must be {NAME_MAX} characters or fewer." in response.text


def test_product_group_create_rejects_overlong_name(client):
    response = client.post(
        "/products/groups/new",
        data={
            "name": "G" * (NAME_MAX + 1),
            "description": "",
            "nominal_code_default": "",
        },
    )

    assert response.status_code == 400
    assert f"Name must be {NAME_MAX} characters or fewer." in response.text


def test_lookup_create_rejects_overlong_name(client):
    response = client.post(
        "/lookups/drivers/new",
        data={"name": "D" * (NAME_MAX + 1)},
    )

    assert response.status_code == 400
    assert f"Name must be {NAME_MAX} characters or fewer." in response.text


def test_customers_list_renders_truncation_markup(client, db_session):
    customer = Customer(
        account_code="C-TRUNC-1",
        name="Customer Name " * 8,
        invoice_email="billing-team-with-a-very-long-alias@example.com",
    )
    db_session.add(customer)
    db_session.commit()

    response = client.get("/customers")

    assert response.status_code == 200
    assert (
        f'class="truncate truncate--sm" title="{customer.account_code}"'
        in response.text
    )
    assert f'class="truncate truncate--md" title="{customer.name}"' in response.text


def test_products_list_renders_truncation_markup(client, db_session):
    unit = Unit(name="Each Truncation", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-TRUNC-1",
        description="Long product description " * 8,
        unit=unit,
        unit_price=Decimal("1.00"),
    )
    db_session.add_all([unit, product])
    db_session.commit()

    response = client.get("/products")

    expected_description = product.description[:28] + "..."
    assert response.status_code == 200
    assert 'class="table-wrap list-table-wrap"' in response.text
    assert 'class="data-table list-table products-table"' in response.text
    assert 'data-row-link="/products/' in response.text
    assert "<th>Group</th>" not in response.text
    assert "<th>Nominal code</th>" not in response.text
    assert f'class="truncate truncate--sm" title="{product.code}"' in response.text
    assert f'title="{product.description}"' in response.text
    assert expected_description in response.text


def test_lookups_list_renders_truncation_markup(client, db_session):
    driver = Driver(name="Driver Name " * 10, is_active=True)
    db_session.add(driver)
    db_session.commit()

    response = client.get("/lookups/drivers")

    assert response.status_code == 200
    assert f'class="truncate truncate--md" title="{driver.name}"' in response.text


def test_tickets_list_renders_truncation_markup(client, db_session):
    customer = Customer(account_code="C-TICKET-TRUNC", name="Ticket Customer " * 8)
    unit = Unit(name="Ticket Truncation Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-TICKET-TRUNC",
        description="Ticket product description " * 8,
        unit=unit,
        unit_price=Decimal("2.50"),
    )
    vehicle = Vehicle(registration="AB12CDE")
    db_session.add_all([customer, unit, product, vehicle])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-TRUNC-1",
        datetime=datetime(2026, 2, 17, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert (
        f'class="truncate truncate--sm" title="{vehicle.registration}"'
        in response.text
    )
    assert (
        f'class="truncate truncate--md" title="{customer.name}"'
        in response.text
    )
    assert (
        f'class="truncate truncate--lg" title="{product.description}"'
        in response.text
    )
