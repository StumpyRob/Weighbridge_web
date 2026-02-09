from datetime import datetime
from decimal import Decimal
import re

from app.models import (
    Customer,
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    WasteProducerSourceEnum,
)


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def _source_value(value):
    return value.value if hasattr(value, "value") else str(value or "")


def _create_count_product(db_session, code: str) -> Product:
    unit = Unit(name=f"Each-{code}", unit_type="COUNT", is_active=True)
    db_session.add(unit)
    db_session.commit()

    product = Product(
        code=code,
        description=f"Product {code}",
        unit_id=unit.id,
        unit_price=Decimal("10.00"),
    )
    db_session.add(product)
    db_session.commit()
    return product


def test_ticket_edit_uses_same_as_customer_model_for_waste_producer(client, db_session):
    ticket = Ticket(
        ticket_no="T-WP-UI-1",
        datetime=datetime(2026, 2, 9, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="waste_producer_same_as_customer"' in response.text
    assert 'name="waste_producer_name"' in response.text
    assert 'name="waste_producer_address_line_1"' in response.text
    assert 'name="waste_producer_customer_id"' not in response.text


def test_new_quick_ticket_defaults_waste_producer_same_as_customer_checked(client):
    response = client.post("/tickets/new/quick", follow_redirects=True)

    assert response.status_code == 200
    assert re.search(
        r'id="waste_producer_same_as_customer"[^>]*checked', response.text
    )


def test_complete_requires_customer_when_waste_producer_same_as_customer(
    client, db_session
):
    product = _create_count_product(db_session, "P-WP-COMPLETE-1")
    ticket = Ticket(
        ticket_no="T-WP-COMPLETE-1",
        datetime=datetime(2026, 2, 9, 10, 0, 0),
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
            "datetime": "2026-02-09T10:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "product_id": str(product.id),
            "qty": "1",
            "waste_producer_same_as_customer": "on",
        },
    )

    assert response.status_code == 400
    assert "Waste producer is set to same as customer - select a customer." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_complete_requires_manual_waste_producer_name_when_not_same_as_customer(
    client, db_session
):
    customer = Customer(account_code="C-WP-1", name="Waste Producer Customer")
    product = _create_count_product(db_session, "P-WP-COMPLETE-2")
    ticket = Ticket(
        ticket_no="T-WP-COMPLETE-2",
        datetime=datetime(2026, 2, 9, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-09T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
            "waste_producer_same_as_customer_present": "1",
            "waste_producer_name": "",
            "waste_producer_address_line_1": "",
            "waste_producer_address_line_2": "",
            "waste_producer_address_line_3": "",
            "waste_producer_postcode": "",
        },
    )

    assert response.status_code == 400
    assert "Enter waste producer name or tick" in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_save_same_as_customer_copies_snapshot_and_source(client, db_session):
    customer = Customer(
        account_code="C-WP-SAME-1",
        name="Same Source Customer",
        address_line1="1 Station Road",
        address_line2="North Yard",
        city="Leeds",
        postcode="LS1 1AA",
        country="UK",
    )
    ticket = Ticket(
        ticket_no="T-WP-SAME-1",
        datetime=datetime(2026, 2, 9, 12, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-09T12:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": str(customer.id),
            "waste_producer_same_as_customer": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _source_value(ticket.waste_producer_source) == WasteProducerSourceEnum.CUSTOMER.value
    assert ticket.waste_producer_customer_id is None
    assert ticket.waste_producer_name == customer.name
    assert ticket.waste_producer_address is not None
    assert "1 Station Road" in ticket.waste_producer_address
    assert "LS1 1AA" in ticket.waste_producer_address


def test_same_as_customer_renders_blank_manual_fields_while_checked(client, db_session):
    customer = Customer(
        account_code="C-WP-SAME-2",
        name="Render Blank Customer",
        address_line1="22 Canal Street",
        city="Leeds",
        postcode="LS2 7AA",
        country="UK",
    )
    ticket = Ticket(
        ticket_no="T-WP-SAME-2",
        datetime=datetime(2026, 2, 9, 12, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        waste_producer_source=WasteProducerSourceEnum.CUSTOMER.value,
        waste_producer_name=customer.name,
        waste_producer_address="22 Canal Street, Leeds, LS2 7AA, UK",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(customer)
    db_session.commit()
    ticket.customer_id = customer.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert re.search(
        r'id="waste_producer_same_as_customer"[\s\S]*?checked', response.text
    )
    assert 'id="waste_producer_name" name="waste_producer_name" value=""' in response.text
    assert 'id="waste_producer_address_line_1" name="waste_producer_address_line_1" value=""' in response.text
    assert 'id="waste_producer_postcode" name="waste_producer_postcode" value=""' in response.text


def test_save_manual_waste_producer_copies_manual_snapshot_and_source(client, db_session):
    ticket = Ticket(
        ticket_no="T-WP-MANUAL-1",
        datetime=datetime(2026, 2, 9, 13, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-09T13:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "waste_producer_same_as_customer_present": "1",
            "waste_producer_name": "Manual Producer Ltd",
            "waste_producer_address_line_1": "10 River Way",
            "waste_producer_address_line_2": "Docklands",
            "waste_producer_address_line_3": "",
            "waste_producer_postcode": "E16 2AB",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _source_value(ticket.waste_producer_source) == WasteProducerSourceEnum.MANUAL.value
    assert ticket.waste_producer_customer_id is None
    assert ticket.waste_producer_name == "Manual Producer Ltd"
    assert ticket.waste_producer_address == "10 River Way, Docklands, E16 2AB"
