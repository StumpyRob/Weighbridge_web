import re
from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    EwcCode,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _make_ewc(code_6: str, description: str, hazardous: bool = False) -> EwcCode:
    return EwcCode(
        code_6=code_6,
        code_display=f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}",
        description=description,
        hazardous=hazardous,
        active=True,
        source_file="test",
        imported_at=datetime(2026, 1, 1, 0, 0, 0),
    )


def _make_weight_product(
    *, code: str, description: str, unit: Unit, unit_price: Decimal, ewc: EwcCode | None = None
) -> Product:
    return Product(
        code=code,
        description=description,
        unit=unit,
        unit_price=unit_price,
        ewc_code=ewc,
    )


def _waste_complete_payload(
    *,
    customer_id: int,
    destination_id: int,
    product_id: int,
    ewc_code: str = "",
    ewc_manual_override: str = "0",
) -> dict[str, str]:
    return {
        "action": "complete",
        "datetime": "2026-02-12T09:00",
        "direction": "INWARD",
        "transaction_type": "WASTEIN",
        "gross_kg": "2500",
        "tare_kg": "1400",
        "customer_id": str(customer_id),
        "destination_id": str(destination_id),
        "product_id": str(product_id),
        "reg": "AB12CDE",
        "ewc_code": ewc_code,
        "ewc_manual_override": ewc_manual_override,
    }


def test_waste_complete_requires_ticket_ewc_when_missing_everywhere(client, db_session):
    customer = Customer(account_code="C-EWC-MISS", name="EWC Missing Customer")
    destination = Destination(name="EWC Missing Destination")
    unit = Unit(name="EWC Missing Unit", unit_type="WEIGHT", is_active=True)
    product = _make_weight_product(
        code="P-EWC-MISS",
        description="No default EWC product",
        unit=unit,
        unit_price=Decimal("15.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-MISS-1",
        datetime=datetime(2026, 2, 12, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data=_waste_complete_payload(
            customer_id=customer.id,
            destination_id=destination.id,
            product_id=product.id,
        ),
    )

    assert response.status_code == 400
    assert "EWC code is required to complete a waste ticket." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_waste_complete_requires_producer_name_when_manual_source_selected(
    client, db_session
):
    customer = Customer(account_code="C-EWC-PROD-REQ", name="Producer Required Customer")
    destination = Destination(name="Producer Required Destination")
    unit = Unit(name="Producer Required Unit", unit_type="WEIGHT", is_active=True)
    manual_ewc = _make_ewc("121212", "Producer Required EWC")
    product = _make_weight_product(
        code="P-EWC-PROD-REQ",
        description="Producer Required Product",
        unit=unit,
        unit_price=Decimal("15.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-PROD-REQ-1",
        datetime=datetime(2026, 2, 12, 9, 2, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, manual_ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            **_waste_complete_payload(
                customer_id=customer.id,
                destination_id=destination.id,
                product_id=product.id,
                ewc_code="12 12 12",
                ewc_manual_override="1",
            ),
            "waste_producer_same_as_customer_present": "1",
            "waste_producer_name": "   ",
        },
    )

    assert response.status_code == 400
    assert "Waste producer name is required to complete a waste ticket." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_waste_save_allows_missing_producer_name_when_manual_source_selected(
    client, db_session
):
    customer = Customer(account_code="C-EWC-PROD-SAVE", name="Producer Save Customer")
    destination = Destination(name="Producer Save Destination")
    unit = Unit(name="Producer Save Unit", unit_type="WEIGHT", is_active=True)
    product = _make_weight_product(
        code="P-EWC-PROD-SAVE",
        description="Producer Save Product",
        unit=unit,
        unit_price=Decimal("15.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-PROD-SAVE-1",
        datetime=datetime(2026, 2, 12, 9, 3, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-12T09:03",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": str(customer.id),
            "destination_id": str(destination.id),
            "product_id": str(product.id),
            "gross_kg": "2500",
            "tare_kg": "1400",
            "reg": "AB12CDE",
            "waste_producer_same_as_customer_present": "1",
            "waste_producer_name": "   ",
            "ewc_code": "",
            "ewc_manual_override": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?saved=1")
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_waste_save_allows_missing_ewc_when_missing_everywhere(client, db_session):
    customer = Customer(account_code="C-EWC-SAVE-MISS", name="EWC Save Missing Customer")
    destination = Destination(name="EWC Save Missing Destination")
    unit = Unit(name="EWC Save Missing Unit", unit_type="WEIGHT", is_active=True)
    product = _make_weight_product(
        code="P-EWC-SAVE-MISS",
        description="No default EWC save product",
        unit=unit,
        unit_price=Decimal("15.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-SAVE-MISS-1",
        datetime=datetime(2026, 2, 12, 9, 5, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-12T09:05",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": str(customer.id),
            "destination_id": str(destination.id),
            "product_id": str(product.id),
            "gross_kg": "2500",
            "tare_kg": "1400",
            "reg": "AB12CDE",
            "ewc_code": "",
            "ewc_manual_override": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?saved=1")
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert ticket.ewc_code_6 is None


def test_waste_complete_allows_manual_ticket_ewc_when_product_has_none(client, db_session):
    customer = Customer(account_code="C-EWC-MAN", name="EWC Manual Customer")
    destination = Destination(name="EWC Manual Destination")
    unit = Unit(name="EWC Manual Unit", unit_type="WEIGHT", is_active=True)
    manual_ewc = _make_ewc("111111", "Manual selected EWC")
    product = _make_weight_product(
        code="P-EWC-MAN",
        description="Manual EWC product",
        unit=unit,
        unit_price=Decimal("20.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-MAN-1",
        datetime=datetime(2026, 2, 12, 9, 10, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, manual_ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data=_waste_complete_payload(
            customer_id=customer.id,
            destination_id=destination.id,
            product_id=product.id,
            ewc_code="11 11 11",
            ewc_manual_override="1",
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.ewc_code_6 == "111111"
    assert ticket.ewc_code_display == "11 11 11"
    assert ticket.ewc_description == "Manual selected EWC"
    assert ticket.ewc_manual_override is True


def test_waste_complete_autofills_ticket_ewc_from_product_default(client, db_session):
    customer = Customer(account_code="C-EWC-AUTO", name="EWC Auto Customer")
    destination = Destination(name="EWC Auto Destination")
    unit = Unit(name="EWC Auto Unit", unit_type="WEIGHT", is_active=True)
    default_ewc = _make_ewc("222222", "Default product EWC")
    product = _make_weight_product(
        code="P-EWC-AUTO",
        description="Auto-fill product",
        unit=unit,
        unit_price=Decimal("22.00"),
        ewc=default_ewc,
    )
    ticket = Ticket(
        ticket_no="T-EWC-AUTO-1",
        datetime=datetime(2026, 2, 12, 9, 20, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, default_ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data=_waste_complete_payload(
            customer_id=customer.id,
            destination_id=destination.id,
            product_id=product.id,
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert ticket.ewc_code_6 == "222222"
    assert ticket.ewc_code_display == "22 22 22"
    assert ticket.ewc_description == "Default product EWC"
    assert ticket.ewc_manual_override is False


def test_waste_save_autofills_ticket_ewc_from_product_default(client, db_session):
    unit = Unit(name="EWC Save Unit", unit_type="WEIGHT", is_active=True)
    default_ewc = _make_ewc("333333", "Save default EWC")
    product = _make_weight_product(
        code="P-EWC-SAVE",
        description="Save auto-fill product",
        unit=unit,
        unit_price=Decimal("10.00"),
        ewc=default_ewc,
    )
    ticket = Ticket(
        ticket_no="T-EWC-SAVE-1",
        datetime=datetime(2026, 2, 12, 9, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, default_ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-12T09:30",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "product_id": str(product.id),
            "ewc_code": "",
            "ewc_manual_override": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.ewc_code_6 == "333333"
    assert ticket.ewc_code_display == "33 33 33"
    assert ticket.ewc_manual_override is False


def test_waste_save_does_not_block_invalid_ewc_format(client, db_session):
    unit = Unit(name="EWC Save Invalid Unit", unit_type="WEIGHT", is_active=True)
    default_ewc = _make_ewc("777777", "Save invalid format default EWC")
    product = _make_weight_product(
        code="P-EWC-SAVE-INVALID",
        description="Save invalid EWC format product",
        unit=unit,
        unit_price=Decimal("10.00"),
        ewc=default_ewc,
    )
    ticket = Ticket(
        ticket_no="T-EWC-SAVE-INVALID-1",
        datetime=datetime(2026, 2, 12, 9, 35, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, default_ewc, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-12T09:35",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "product_id": str(product.id),
            "ewc_code": "12",
            "ewc_manual_override": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert ticket.ewc_code_6 == "777777"
    assert ticket.ewc_code_display == "77 77 77"


def test_waste_complete_blocks_invalid_ewc_format(client, db_session):
    customer = Customer(account_code="C-EWC-FMT", name="EWC Format Customer")
    destination = Destination(name="EWC Format Destination")
    unit = Unit(name="EWC Format Unit", unit_type="WEIGHT", is_active=True)
    product = _make_weight_product(
        code="P-EWC-FMT",
        description="Format check product",
        unit=unit,
        unit_price=Decimal("20.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-FMT-1",
        datetime=datetime(2026, 2, 12, 9, 36, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, destination, unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data=_waste_complete_payload(
            customer_id=customer.id,
            destination_id=destination.id,
            product_id=product.id,
            ewc_code="12",
            ewc_manual_override="1",
        ),
    )

    assert response.status_code == 400
    assert "EWC code must be 6 digits (for example, 01 01 01)." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_sale_complete_does_not_require_ewc(client, db_session):
    customer = Customer(account_code="C-EWC-SALE", name="Sale EWC Customer")
    count_unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-EWC-SALE",
        description="Sale without EWC",
        unit=count_unit,
        unit_price=Decimal("5.00"),
    )
    ticket = Ticket(
        ticket_no="T-EWC-SALE-1",
        datetime=datetime(2026, 2, 12, 9, 40, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, count_unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-12T09:40",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "2",
            "ewc_code": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value


def test_ticket_ewc_snapshot_stays_unchanged_if_product_default_changes(client, db_session):
    customer = Customer(account_code="C-EWC-SNAP", name="Snapshot Customer")
    destination = Destination(name="Snapshot Destination")
    unit = Unit(name="Snapshot Unit", unit_type="WEIGHT", is_active=True)
    ewc_initial = _make_ewc("444444", "Initial product EWC")
    ewc_new = _make_ewc("555555", "Changed product EWC")
    product = _make_weight_product(
        code="P-EWC-SNAP",
        description="Snapshot product",
        unit=unit,
        unit_price=Decimal("30.00"),
        ewc=ewc_initial,
    )
    ticket = Ticket(
        ticket_no="T-EWC-SNAP-1",
        datetime=datetime(2026, 2, 12, 9, 50, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all(
        [customer, destination, unit, ewc_initial, ewc_new, product, ticket]
    )
    db_session.commit()

    complete_response = client.post(
        f"/tickets/{ticket.id}",
        data=_waste_complete_payload(
            customer_id=customer.id,
            destination_id=destination.id,
            product_id=product.id,
        ),
        follow_redirects=False,
    )

    assert complete_response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.ewc_code_6 == "444444"
    assert ticket.ewc_code_display == "44 44 44"

    product.ewc_code = ewc_new
    db_session.commit()

    db_session.refresh(ticket)
    assert ticket.ewc_code_6 == "444444"
    assert ticket.ewc_code_display == "44 44 44"
    assert ticket.ewc_description == "Initial product EWC"


def test_ticket_edit_hides_ewc_field_for_sale(client, db_session):
    ticket = Ticket(
        ticket_no="T-EWC-UI-SALE-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
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
    assert re.search(
        r'id="waste-compliance-section"[^>]*style="display:none"',
        response.text,
    )


def test_ticket_edit_does_not_show_ewc_required_hint_for_waste(client, db_session):
    ticket = Ticket(
        ticket_no="T-EWC-UI-WASTE-1",
        datetime=datetime(2026, 2, 12, 10, 5, 0),
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
    assert 'id="ewc-ticket-field"' in response.text
    assert "Required for waste tickets." not in response.text
    assert "No default EWC on selected product." not in response.text


def test_ticket_edit_ewc_datalist_allows_description_prefix_search(client, db_session):
    ewc = _make_ewc("888888", "Acids and alkalis")
    ticket = Ticket(
        ticket_no="T-EWC-UI-LIST-1",
        datetime=datetime(2026, 2, 12, 10, 6, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([ewc, ticket])
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="ewc_code_list"' in response.text
    assert 'value="88 88 88"' in response.text
    assert 'value="Acids and alkalis - 88 88 88"' in response.text
    assert 'data-code-display="88 88 88"' in response.text


def test_ticket_edit_hidden_default_ewc_code_uses_digits_only(client, db_session):
    unit = Unit(name="EWC Hidden Unit", unit_type="WEIGHT", is_active=True)
    default_ewc = _make_ewc("666666", "Hidden default EWC")
    product = _make_weight_product(
        code="P-EWC-HIDDEN",
        description="Hidden EWC Product",
        unit=unit,
        unit_price=Decimal("12.00"),
        ewc=default_ewc,
    )
    ticket = Ticket(
        ticket_no="T-EWC-HIDDEN-1",
        datetime=datetime(2026, 2, 12, 10, 10, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, default_ewc, product])
    db_session.commit()
    ticket.product_id = product.id
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert re.search(
        r'id="ewc_product_default_code_6"[^>]*value="666666"',
        response.text,
    )
    assert re.search(
        r'id="ewc_product_default_display"[^>]*value="66 66 66"',
        response.text,
    )
