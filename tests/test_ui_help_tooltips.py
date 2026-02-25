from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
)


def test_product_edit_uses_tooltips_and_removes_old_nominal_helper(client, db_session):
    unit = Unit(name="Tooltip Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-HELP-1",
        description="Tooltip Product",
        unit=unit,
        unit_price=Decimal("9.99"),
    )
    db_session.add_all([unit, product])
    db_session.commit()

    response = client.get(f"/products/{product.id}")

    assert response.status_code == 200
    assert 'id="product-group-help"' in response.text
    assert 'id="product-nominal-code-help"' in response.text
    assert 'id="product-ewc-code-help"' in response.text
    assert 'id="product-default-destination-help"' in response.text
    assert 'id="product-sales-only-help"' in response.text
    assert 'id="product-hazardous-help"' in response.text
    assert "Leave blank to inherit the Product Group default nominal code." in response.text
    assert "If blank, will use Product Group default (if set)." not in response.text


def test_customer_edit_shows_tooltips_for_billing_and_flags(client, db_session):
    customer = Customer(account_code="C-HELP-1", name="Tooltip Customer")
    db_session.add(customer)
    db_session.commit()

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert 'id="customer-invoice-frequency-help"' in response.text
    assert 'id="customer-payment-terms-days-help"' in response.text
    assert 'id="customer-payment-terms-help"' in response.text
    assert 'id="customer-on-stop-help"' in response.text
    assert 'id="customer-do-not-invoice-help"' in response.text
    assert 'id="customer-must-have-po-help"' in response.text
    assert "Used to suggest invoice date ranges when generating invoices." in response.text
    assert "Days from invoice date used to calculate due date." in response.text


def test_ticket_edit_shows_tooltips_and_no_inline_dont_invoice_hint(client, db_session):
    ticket = Ticket(
        ticket_no="T-HELP-1",
        datetime=datetime(2026, 2, 17, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="ticket-direction-help"' in response.text
    assert 'id="ticket-transaction-type-help"' in response.text
    assert 'id="ticket-walk-in-sale-help"' in response.text
    assert 'id="ticket-dont-invoice-help"' in response.text
    assert 'id="ticket-yard-help"' in response.text
    assert 'id="ticket-area-help"' in response.text
    assert 'id="ticket-waste-producer-help"' in response.text
    assert 'id="ticket-ewc-code-help"' in response.text
    assert 'id="ticket-ewc-hazard-help"' in response.text
    assert 'id="ticket-void-reason-help"' in response.text
    assert "Use for cash/card counter sales. No customer invoice generated." in response.text
    assert "Locked on for walk-in sale." not in response.text


def test_vehicle_pages_show_tooltips_for_defaults_and_tares(client, db_session):
    vehicle = Vehicle(registration="VH-HELP-1")
    db_session.add(vehicle)
    db_session.commit()

    list_response = client.get("/vehicles")
    assert list_response.status_code == 200
    assert 'id="vehicle-list-registration-search-help"' in list_response.text
    assert 'id="vehicle-list-default-tare-help"' not in list_response.text
    assert 'id="vehicle-list-overweight-threshold-help"' not in list_response.text

    new_response = client.get("/vehicles/new")
    assert new_response.status_code == 200
    assert 'id="vehicle-default-tare-help"' in new_response.text
    assert 'id="vehicle-overweight-threshold-help"' in new_response.text
    assert 'id="vehicle-ticket-defaults-help"' in new_response.text
    assert 'id="vehicle-default-customer-help"' in new_response.text
    assert 'id="vehicle-default-haulier-help"' in new_response.text
    assert 'id="vehicle-default-driver-help"' in new_response.text
    assert "Typical Fleet Associations" not in new_response.text

    edit_response = client.get(f"/vehicles/{vehicle.id}")
    assert edit_response.status_code == 200
    assert 'id="vehicle-per-container-tares-help"' in edit_response.text
