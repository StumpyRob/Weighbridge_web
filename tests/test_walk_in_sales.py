import re
from datetime import datetime
from decimal import Decimal

from app.models import (
    Container,
    Customer,
    Destination,
    DirectionEnum,
    Driver,
    Haulier,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _make_sale_product(db_session, *, code: str) -> Product:
    unit = Unit(name=f"Unit-{code}", unit_type="COUNT", is_active=True)
    product = Product(
        code=code,
        description=f"Product {code}",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([unit, product])
    db_session.commit()
    return product


def test_sale_walk_in_sale_can_complete_without_customer(client, db_session):
    product = _make_sale_product(db_session, code="P-WALK-COUNT-1")
    ticket = Ticket(
        ticket_no="T-WALK-SALE-1",
        datetime=datetime(2026, 2, 17, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-17T09:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "product_id": str(product.id),
            "qty": "2",
            "walk_in_sale": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.walk_in_sale is True
    assert ticket.customer_id is None
    assert ticket.dont_invoice is True


def test_sale_without_walk_in_sale_still_requires_customer(client, db_session):
    product = _make_sale_product(db_session, code="P-WALK-COUNT-2")
    ticket = Ticket(
        ticket_no="T-WALK-SALE-2",
        datetime=datetime(2026, 2, 17, 9, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-17T09:30",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "product_id": str(product.id),
            "qty": "1",
        },
    )

    assert response.status_code == 400
    assert "Customer is required to complete a sale ticket." in response.text


def test_walk_in_sale_forces_dont_invoice_and_clears_logistics(client, db_session):
    customer = Customer(account_code="C-WALK-3", name="Walk Customer 3")
    haulier = Haulier(name="Walk Haulier")
    driver = Driver(name="Walk Driver")
    container = Container(name="Walk Container")
    destination = Destination(name="Walk Destination")
    product = _make_sale_product(db_session, code="P-WALK-COUNT-3")
    db_session.add_all([customer, haulier, driver, container, destination])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-WALK-SALE-3",
        datetime=datetime(2026, 2, 17, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        haulier_id=haulier.id,
        driver_id=driver.id,
        container_id=container.id,
        destination_id=destination.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-17T10:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
            "haulier_id": str(haulier.id),
            "driver_id": str(driver.id),
            "container_id": str(container.id),
            "destination_id": str(destination.id),
            "walk_in_sale": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.walk_in_sale is True
    assert ticket.customer_id is None
    assert ticket.dont_invoice is True
    assert ticket.haulier_id is None
    assert ticket.driver_id is None
    assert ticket.container_id is None
    assert ticket.destination_id is None
    assert ticket.area_id is None


def test_walk_in_sale_ui_gates_customer_and_logistics_fields(client, db_session):
    sale_ticket = Ticket(
        ticket_no="T-WALK-UI-SALE",
        datetime=datetime(2026, 2, 17, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        walk_in_sale=True,
        dont_invoice=True,
        paid=False,
    )
    waste_ticket = Ticket(
        ticket_no="T-WALK-UI-WASTE",
        datetime=datetime(2026, 2, 17, 11, 5, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([sale_ticket, waste_ticket])
    db_session.commit()

    sale_response = client.get(f"/tickets/{sale_ticket.id}")
    waste_response = client.get(f"/tickets/{waste_ticket.id}")

    assert sale_response.status_code == 200
    assert "Walk-in sale (no invoice)" in sale_response.text
    assert "Use for cash/card counter sales. No customer invoice generated." in sale_response.text
    assert re.search(
        r'id="customer-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="customer_id"[^>]*disabled',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="participant-haulier-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="logistics-driver-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="logistics-container-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="logistics-destination-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None
    assert re.search(
        r'id="logistics-area-field"[^>]*style="display:none"',
        sale_response.text,
    ) is not None

    assert waste_response.status_code == 200
    assert re.search(
        r'id="walk-in-sale-field"[^>]*style="display:none"',
        waste_response.text,
    ) is not None


def test_walk_in_sale_keeps_vehicle_reg_and_yard_editable(client, db_session):
    ticket = Ticket(
        ticket_no="T-WALK-UI-EDITABLE-1",
        datetime=datetime(2026, 2, 17, 11, 15, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        walk_in_sale=True,
        dont_invoice=True,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    vehicle_reg_input = re.search(r'<input[^>]*id="vehicle_reg"[^>]*>', response.text)
    assert vehicle_reg_input is not None
    assert "disabled" not in vehicle_reg_input.group(0).lower()

    yard_input = re.search(r'<input[^>]*id="yard_id"[^>]*>', response.text)
    assert yard_input is not None
    assert "disabled" not in yard_input.group(0).lower()


def test_ticket_list_shows_walk_in_badge_and_filter(client, db_session):
    walk_in_ticket = Ticket(
        ticket_no="T-WALK-LIST-1",
        datetime=datetime(2026, 2, 17, 12, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        walk_in_sale=True,
        dont_invoice=True,
        paid=False,
    )
    regular_ticket = Ticket(
        ticket_no="T-WALK-LIST-2",
        datetime=datetime(2026, 2, 17, 12, 1, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        walk_in_sale=False,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([walk_in_ticket, regular_ticket])
    db_session.commit()

    all_rows = client.get("/tickets")
    assert all_rows.status_code == 200
    assert walk_in_ticket.ticket_no in all_rows.text
    assert regular_ticket.ticket_no in all_rows.text
    assert "Walk-in" in all_rows.text

    filtered = client.get("/tickets", params={"walk_in_sale_only": "1"})
    assert filtered.status_code == 200
    assert walk_in_ticket.ticket_no in filtered.text
    assert regular_ticket.ticket_no not in filtered.text


def test_walk_in_sale_ticket_excluded_from_invoice_generation(client, db_session):
    customer = Customer(account_code="C-WALK-INV-1", name="Walk Invoice Exclusion")
    product = _make_sale_product(db_session, code="P-WALK-INV-1")
    db_session.add(customer)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-WALK-INV-1",
        datetime=datetime(2026, 2, 17, 13, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        walk_in_sale=True,
        dont_invoice=False,
        product_id=product.id,
        qty=Decimal("1.000"),
        unit_price=Decimal("10.00"),
        total=Decimal("10.00"),
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert response.status_code == 200
    assert "No tickets found." in response.text
