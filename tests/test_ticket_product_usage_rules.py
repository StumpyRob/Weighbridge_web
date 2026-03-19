from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def test_waste_ticket_dropdown_excludes_sales_only_products(client, db_session):
    unit = Unit(name="Usage Rule Unit", unit_type="COUNT", is_active=True)
    sales_only_product = Product(
        code="P-USAGE-SALES-1",
        description="High Vis Retail",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    waste_product = Product(
        code="P-USAGE-WASTE-1",
        description="General Waste Product",
        sales_only=False,
        unit=unit,
        unit_price=Decimal("120.00"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-DROPDOWN-1",
        datetime=datetime(2026, 2, 10, 8, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sales_only_product, waste_product, ticket])
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "General Waste Product" in response.text
    assert "High Vis Retail" not in response.text


def test_waste_ticket_legacy_sales_only_product_shows_warning(client, db_session):
    unit = Unit(name="Usage Rule Unit 2", unit_type="COUNT", is_active=True)
    sales_only_product = Product(
        code="P-USAGE-SALES-2",
        description="Legacy Retail Product",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-DROPDOWN-2",
        datetime=datetime(2026, 2, 10, 8, 15, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        product_id=sales_only_product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sales_only_product])
    db_session.commit()
    ticket.product_id = sales_only_product.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Legacy Retail Product (sales only)" in response.text
    assert "Selected product is sales-only and cannot be used on waste tickets." in response.text


def test_waste_ticket_save_rejects_sales_only_product(client, db_session):
    unit = Unit(name="Usage Rule Unit 3", unit_type="COUNT", is_active=True)
    sales_only_product = Product(
        code="P-USAGE-SALES-3",
        description="Sales Only Waste Save",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-SAVE-1",
        datetime=datetime(2026, 2, 10, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sales_only_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-10T09:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "product_id": str(sales_only_product.id),
        },
    )

    assert response.status_code == 400
    assert "This product is sales-only and cannot be used on waste tickets." in response.text
    db_session.refresh(ticket)
    assert ticket.product_id is None
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_waste_ticket_complete_rejects_sales_only_product(client, db_session):
    unit = Unit(name="Usage Rule Unit 4", unit_type="COUNT", is_active=True)
    customer = Customer(account_code="C-USAGE-STOP", name="Usage Customer")
    destination = Destination(name="Usage Destination")
    sales_only_product = Product(
        code="P-USAGE-SALES-4",
        description="Sales Only Waste Complete",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-COMPLETE-1",
        datetime=datetime(2026, 2, 10, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, customer, destination, sales_only_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": str(customer.id),
            "destination_id": str(destination.id),
            "product_id": str(sales_only_product.id),
            "qty": "1",
            "reg": "AB12CDE",
        },
    )

    assert response.status_code == 400
    assert "This product is sales-only and cannot be used on waste tickets." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert ticket.product_id is None


def test_product_defaults_rejects_sales_only_product_for_waste(client, db_session):
    unit = Unit(name="Usage Rule Unit 5", unit_type="COUNT", is_active=True)
    sales_only_product = Product(
        code="P-USAGE-SALES-5",
        description="Sales Only Defaults",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-DEFAULTS-1",
        datetime=datetime(2026, 2, 10, 10, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sales_only_product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={sales_only_product.id}"
        f"&ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 400
    assert "This product is sales-only and cannot be used on waste tickets." in response.text


def test_sale_ticket_allows_sales_only_product_with_correct_pricing(client, db_session):
    unit = Unit(name="Usage Rule Unit 6", unit_type="COUNT", is_active=True)
    customer = Customer(account_code="C-USAGE-SALE", name="Sale Customer")
    sales_only_product = Product(
        code="P-USAGE-SALES-6",
        description="Sales Only Allowed",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-SALE-1",
        datetime=datetime(2026, 2, 10, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, customer, sales_only_product, ticket])
    db_session.commit()

    save_response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-10T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(sales_only_product.id),
            "qty": "2",
            "unit_price": "14.99",
        },
        follow_redirects=False,
    )
    assert save_response.status_code == 303

    complete_response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(sales_only_product.id),
            "qty": "2",
            "unit_price": "14.99",
        },
        follow_redirects=False,
    )

    assert complete_response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.product_id == sales_only_product.id
    assert Decimal(str(ticket.total)) == Decimal("29.98")


def test_sale_ticket_save_rejects_waste_only_product(client, db_session):
    unit = Unit(name="Usage Rule Unit 6B", unit_type="COUNT", is_active=True)
    waste_product = Product(
        code="P-USAGE-WASTE-SALE-1",
        description="Waste Only Sale Block",
        product_type="waste",
        sales_only=False,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-SALE-BLOCK-1",
        datetime=datetime(2026, 2, 10, 11, 10, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, waste_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-10T11:10",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "product_id": str(waste_product.id),
        },
    )

    assert response.status_code == 400
    assert "This product is waste-only and cannot be used on sale tickets." in response.text
    db_session.refresh(ticket)
    assert ticket.product_id is None


def test_waste_ticket_save_rejects_sale_product_type(client, db_session):
    unit = Unit(name="Usage Rule Unit 6C", unit_type="COUNT", is_active=True)
    sale_product = Product(
        code="P-USAGE-SALE-WASTE-1",
        description="Sale Product Waste Block",
        product_type="sale",
        sales_only=False,
        unit=unit,
        unit_price=Decimal("11.25"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-WASTE-BLOCK-1",
        datetime=datetime(2026, 2, 10, 11, 20, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sale_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-10T11:20",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "product_id": str(sale_product.id),
        },
    )

    assert response.status_code == 400
    assert "This product is sales-only and cannot be used on waste tickets." in response.text
    db_session.refresh(ticket)
    assert ticket.product_id is None


def test_transaction_type_change_refreshes_product_options_without_save(client, db_session):
    unit = Unit(name="Usage Rule Unit 7", unit_type="COUNT", is_active=True)
    sales_only_product = Product(
        code="P-USAGE-SALES-7",
        description="Live Retail Product",
        product_type="sale",
        sales_only=True,
        unit=unit,
        unit_price=Decimal("14.99"),
    )
    waste_product = Product(
        code="P-USAGE-WASTE-7",
        description="Live Waste Product",
        product_type="waste",
        sales_only=False,
        unit=unit,
        unit_price=Decimal("100.00"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-LIVE-1",
        datetime=datetime(2026, 2, 10, 11, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, sales_only_product, waste_product, ticket])
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert 'hx-get="/tickets/product-options"' in page.text
    assert 'hx-trigger="change from:#transaction_type"' in page.text
    assert 'hx-include="#transaction_type"' in page.text
    assert 'hx-include="#transaction_type,#product_id"' not in page.text

    sale_options = client.get(
        "/tickets/product-options"
        f"?ticket_id={ticket.id}"
        "&transaction_type=SALE"
        "&product_id="
    )
    waste_options = client.get(
        "/tickets/product-options"
        f"?ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&product_id="
    )

    assert sale_options.status_code == 200
    assert waste_options.status_code == 200
    assert "Live Retail Product" in sale_options.text
    assert "Live Retail Product" not in waste_options.text
    assert "Live Waste Product" in waste_options.text
    assert "Live Waste Product" not in sale_options.text


def test_product_options_empty_state_message_when_no_type_matches(client, db_session):
    unit = Unit(name="Usage Rule Unit 8", unit_type="COUNT", is_active=True)
    waste_product = Product(
        code="P-USAGE-WASTE-8",
        description="Only Waste Product",
        product_type="waste",
        sales_only=False,
        unit=unit,
        unit_price=Decimal("55.00"),
    )
    ticket = Ticket(
        ticket_no="T-USAGE-EMPTY-1",
        datetime=datetime(2026, 2, 10, 12, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, waste_product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-options"
        f"?ticket_id={ticket.id}"
        "&transaction_type=SALE"
        "&product_id="
    )

    assert response.status_code == 200
    assert "No sale products available." in response.text
    assert "Only Waste Product" not in response.text


def test_final_disposal_sale_requires_destination_to_complete(client, db_session):
    unit = Unit(name="Usage Rule Unit 9", unit_type="COUNT", is_active=True)
    customer = Customer(account_code="C-USAGE-FINAL-1", name="Final Disposal Customer")
    final_disposal_product = Product(
        code="P-USAGE-FINAL-1",
        description="Final Disposal Product",
        product_type="sale",
        unit=unit,
        unit_price=Decimal("45.00"),
        final_disposal=True,
        used_on_site=False,
    )
    ticket = Ticket(
        ticket_no="T-USAGE-FINAL-1",
        datetime=datetime(2026, 2, 10, 12, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, customer, final_disposal_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T12:30",
            "direction": "OUTWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(final_disposal_product.id),
            "qty": "1",
            "unit_price": "45.00",
        },
    )

    assert response.status_code == 400
    assert (
        "Destination site/company is required to complete tickets for final disposal products."
        in response.text
    )
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert ticket.destination_id is None


def test_final_disposal_sale_uses_product_default_destination(client, db_session):
    unit = Unit(name="Usage Rule Unit 10", unit_type="COUNT", is_active=True)
    customer = Customer(account_code="C-USAGE-FINAL-2", name="Final Disposal Customer 2")
    destination = Destination(name="Final Disposal Site")
    final_disposal_product = Product(
        code="P-USAGE-FINAL-2",
        description="Final Disposal Product Default Dest",
        product_type="sale",
        unit=unit,
        unit_price=Decimal("50.00"),
        final_disposal=True,
        used_on_site=False,
        default_destination=destination,
    )
    ticket = Ticket(
        ticket_no="T-USAGE-FINAL-2",
        datetime=datetime(2026, 2, 10, 12, 45, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, customer, destination, final_disposal_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T12:45",
            "direction": "OUTWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(final_disposal_product.id),
            "qty": "1",
            "unit_price": "50.00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.destination_id == destination.id


def test_used_on_site_product_not_forced_to_capture_destination(client, db_session):
    unit = Unit(name="Usage Rule Unit 11", unit_type="COUNT", is_active=True)
    customer = Customer(account_code="C-USAGE-SITE-1", name="Used On Site Customer")
    used_on_site_product = Product(
        code="P-USAGE-SITE-1",
        description="Used On Site Product",
        product_type="sale",
        unit=unit,
        unit_price=Decimal("35.00"),
        final_disposal=True,
        used_on_site=True,
    )
    ticket = Ticket(
        ticket_no="T-USAGE-SITE-1",
        datetime=datetime(2026, 2, 10, 13, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, customer, used_on_site_product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T13:00",
            "direction": "OUTWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(used_on_site_product.id),
            "qty": "1",
            "unit_price": "35.00",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.destination_id is None


def test_used_on_site_hint_is_visible_on_ticket_edit(client, db_session):
    unit = Unit(name="Usage Rule Unit 12", unit_type="COUNT", is_active=True)
    used_on_site_product = Product(
        code="P-USAGE-SITE-2",
        description="Used On Site Product Visible",
        product_type="sale",
        unit=unit,
        unit_price=Decimal("15.00"),
        used_on_site=True,
    )
    ticket = Ticket(
        ticket_no="T-USAGE-SITE-2",
        datetime=datetime(2026, 2, 10, 13, 15, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, used_on_site_product])
    db_session.commit()
    ticket.product_id = used_on_site_product.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert (
        "Used on site product selected. Internal-use flow; disposal transfer paperwork is not required by this flag."
        in response.text
    )
