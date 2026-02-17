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
)


def _input_tag(html: str, input_id: str) -> str:
    match = re.search(rf'<input[^>]*id="{input_id}"[^>]*>', html)
    assert match is not None
    return match.group(0)


def _input_value(html: str, input_id: str) -> str:
    tag = _input_tag(html, input_id)
    match = re.search(r'value="([^"]*)"', tag)
    assert match is not None
    return match.group(1)


def _input_tags(html: str, input_id: str) -> list[str]:
    return re.findall(rf'<input[^>]*id="{input_id}"[^>]*>', html)


def test_product_defaults_tolerates_duplicate_product_id_query_params(client, db_session):
    unit = Unit(name="Defaults Unit 1", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-1",
        description="Defaults Product",
        unit=unit,
        unit_price=Decimal("9.50"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-1",
        datetime=datetime(2026, 2, 9, 14, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        "&product_id="
        f"&ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 200
    assert response.status_code != 422


def test_product_defaults_uses_selected_product_unit_price_when_query_has_value(
    client, db_session
):
    unit = Unit(name="Defaults Unit 2", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-2",
        description="Defaults Product 2",
        unit=unit,
        unit_price=Decimal("12.34"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-2",
        datetime=datetime(2026, 2, 9, 14, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        "&transaction_type=WASTEIN"
        f"&ticket_id={ticket.id}"
        "&weights_product_id=3"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price=9.99"
    )

    assert response.status_code == 200
    assert response.status_code != 500
    assert 'name="unit_price"' in response.text
    assert 'value="12.34"' in response.text


def test_product_defaults_switch_product_refreshes_rate_without_stale_values(
    client, db_session
):
    weight_unit = Unit(name="Topsoil Unit", unit_type="WEIGHT", is_active=True)
    count_unit = Unit(name="HiVis Unit", unit_type="COUNT", is_active=True)
    topsoil = Product(
        code="P-TOPSOIL-1",
        description="TOPSOIL",
        unit=weight_unit,
        unit_price=Decimal("20.00"),
    )
    hivis = Product(
        code="P-HIVIS-1",
        description="HIVIS",
        unit=count_unit,
        unit_price=Decimal("10.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-RATE-SWITCH-1",
        datetime=datetime(2026, 2, 12, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([weight_unit, count_unit, topsoil, hivis, ticket])
    db_session.commit()

    first = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(topsoil.id),
            "ticket_id": str(ticket.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "",
        },
    )
    assert first.status_code == 200
    assert _input_value(first.text, "unit_price") == "20.00"

    second = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(hivis.id),
            "ticket_id": str(ticket.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "20.00",
        },
    )
    assert second.status_code == 200
    assert _input_value(second.text, "unit_price") == "10.00"

    third = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(topsoil.id),
            "ticket_id": str(ticket.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "10.00",
        },
    )
    assert third.status_code == 200
    assert _input_value(third.text, "unit_price") == "20.00"


def test_product_defaults_response_contains_single_rate_input(client, db_session):
    unit = Unit(name="Defaults Unit OOB", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-OOB-1",
        description="Defaults Product OOB",
        unit=unit,
        unit_price=Decimal("17.50"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-OOB-1",
        datetime=datetime(2026, 2, 12, 9, 15, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(product.id),
            "ticket_id": str(ticket.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "",
        },
    )

    assert response.status_code == 200
    unit_price_tags = _input_tags(response.text, "unit_price")
    assert len(unit_price_tags) == 1
    assert 'value="17.50"' in unit_price_tags[0]
    assert 'hx-swap-oob="true"' in unit_price_tags[0]


def test_edit_page_product_change_hx_include_excludes_unit_price(client, db_session):
    ticket = Ticket(
        ticket_no="T-PROD-MARKUP-HX-1",
        datetime=datetime(2026, 2, 9, 16, 15, 0),
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
    match = re.search(
        r'<select[^>]*id="product_id"[^>]*hx-include="([^"]+)"',
        response.text,
    )
    assert match is not None
    assert match.group(1) == "#weights-form,#qty,#transaction_type,#customer_id"
    assert "#unit_price" not in match.group(1)


def test_save_ticket_persists_selected_product(client, db_session):
    unit = Unit(name="Save Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PROD-SAVE-1",
        description="Save Product",
        unit=unit,
        unit_price=Decimal("11.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-SAVE-1",
        datetime=datetime(2026, 2, 9, 15, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-09T15:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "product_id": str(product.id),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(ticket)
    assert ticket.product_id == product.id


def test_edit_page_uses_weights_product_id_hidden_field(client, db_session):
    ticket = Ticket(
        ticket_no="T-PROD-MARKUP-1",
        datetime=datetime(2026, 2, 9, 16, 0, 0),
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
    assert 'name="weights_product_id"' in response.text


def test_save_ticket_rejects_product_with_inactive_unit(client, db_session):
    inactive_unit = Unit(name="Inactive Save Unit", unit_type="COUNT", is_active=False)
    product = Product(
        code="P-PROD-INACTIVE-SAVE-1",
        description="Inactive Save Product",
        unit=inactive_unit,
        unit_price=Decimal("8.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-INACTIVE-SAVE-1",
        datetime=datetime(2026, 2, 10, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([inactive_unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-10T09:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "product_id": str(product.id),
        },
    )

    assert response.status_code == 400
    assert "Product unit is inactive. Choose a different product." in response.text
    db_session.refresh(ticket)
    assert ticket.product_id is None
    assert (
        ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
    ) == TicketStatusEnum.OPEN.value


def test_complete_ticket_rejects_product_with_inactive_unit(client, db_session):
    customer = Customer(account_code="C-PROD-INACTIVE", name="Inactive Unit Customer")
    inactive_unit = Unit(name="Inactive Complete Unit", unit_type="COUNT", is_active=False)
    product = Product(
        code="P-PROD-INACTIVE-COMPLETE-1",
        description="Inactive Complete Product",
        unit=inactive_unit,
        unit_price=Decimal("7.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-INACTIVE-COMPLETE-1",
        datetime=datetime(2026, 2, 10, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, inactive_unit, product, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-10T10:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "1",
        },
    )

    assert response.status_code == 400
    assert "Product unit is inactive. Choose a different product." in response.text
    db_session.refresh(ticket)
    assert (
        ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
    ) == TicketStatusEnum.OPEN.value
    assert ticket.product_id is None


def test_product_defaults_rejects_product_with_inactive_unit(client, db_session):
    inactive_unit = Unit(name="Inactive Defaults Unit", unit_type="COUNT", is_active=False)
    product = Product(
        code="P-PROD-INACTIVE-DEFAULTS-1",
        description="Inactive Defaults Product",
        unit=inactive_unit,
        unit_price=Decimal("6.50"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-INACTIVE-DEFAULTS-1",
        datetime=datetime(2026, 2, 10, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([inactive_unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        f"&ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 400
    assert "Product unit is inactive. Choose a different product." in response.text


def test_product_defaults_weight_product_marks_qty_muted(client, db_session):
    weight_unit = Unit(name="Defaults Weight Unit", unit_type="WEIGHT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-WEIGHT-1",
        description="Defaults Weight Product",
        unit=weight_unit,
        unit_price=Decimal("6.50"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-WEIGHT-1",
        datetime=datetime(2026, 2, 10, 11, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([weight_unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        f"&ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 200
    assert 'class="field muted" id="qty-wrap"' in response.text
    assert re.search(r'<input[^>]*id="qty"[^>]*disabled', response.text)


def test_sale_weight_product_defaults_lock_qty_without_saving(client, db_session):
    weight_unit = Unit(name="Defaults Sale Weight Unit", unit_type="WEIGHT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-SALE-WEIGHT-1",
        description="Defaults Sale Weight Product",
        unit=weight_unit,
        unit_price=Decimal("7.25"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-SALE-WEIGHT-1",
        datetime=datetime(2026, 2, 10, 11, 35, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([weight_unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        f"&ticket_id={ticket.id}"
        "&transaction_type=SALE"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 200
    assert 'class="field muted" id="qty-wrap"' in response.text
    assert re.search(r'<input[^>]*id="qty"[^>]*disabled', response.text)
    assert re.search(r'<input[^>]*name="qty"[^>]*value=""', response.text)
    assert "readonly" not in _input_tag(response.text, "gross_kg")
    assert "readonly" not in _input_tag(response.text, "tare_kg")
    assert "readonly" not in _input_tag(response.text, "readout_kg")


def test_product_defaults_count_product_marks_weights_muted(client, db_session):
    count_unit = Unit(name="Defaults Count Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-PROD-DEFAULTS-COUNT-1",
        description="Defaults Count Product",
        unit=count_unit,
        unit_price=Decimal("6.50"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DEFAULTS-COUNT-1",
        datetime=datetime(2026, 2, 10, 11, 45, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([count_unit, product, ticket])
    db_session.commit()

    response = client.get(
        "/tickets/product-defaults"
        f"?product_id={product.id}"
        f"&ticket_id={ticket.id}"
        "&transaction_type=WASTEIN"
        "&gross_kg=&tare_kg=&net_kg=&readout_kg=&qty=&unit_price="
    )

    assert response.status_code == 200
    assert 'class="form-grid muted" id="weights-wrap"' in response.text
    assert re.search(r'<input[^>]*id="gross_kg"[^>]*readonly', response.text)
    assert "Swap weights not available for COUNT products." in response.text


def test_edit_page_hides_products_with_inactive_units(client, db_session):
    active_unit = Unit(name="Active Dropdown Unit", unit_type="COUNT", is_active=True)
    inactive_unit = Unit(name="Inactive Dropdown Unit", unit_type="COUNT", is_active=False)
    active_product = Product(
        code="P-PROD-DROPDOWN-ACTIVE-1",
        description="Active Dropdown Product",
        unit=active_unit,
        unit_price=Decimal("5.00"),
    )
    inactive_product = Product(
        code="P-PROD-DROPDOWN-INACTIVE-1",
        description="Inactive Dropdown Product",
        unit=inactive_unit,
        unit_price=Decimal("4.00"),
    )
    ticket = Ticket(
        ticket_no="T-PROD-DROPDOWN-1",
        datetime=datetime(2026, 2, 10, 12, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all(
        [active_unit, inactive_unit, active_product, inactive_product, ticket]
    )
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Active Dropdown Product" in response.text
    assert "Inactive Dropdown Product" not in response.text
