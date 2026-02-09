from datetime import datetime
from decimal import Decimal

from app.models import DirectionEnum, Product, Ticket, TicketStatusEnum, TransactionTypeEnum


def test_product_defaults_tolerates_duplicate_product_id_query_params(client, db_session):
    product = Product(
        code="P-PROD-DEFAULTS-1",
        description="Defaults Product",
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
    db_session.add_all([product, ticket])
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


def test_product_defaults_tolerates_numeric_unit_price_query_value(client, db_session):
    product = Product(
        code="P-PROD-DEFAULTS-2",
        description="Defaults Product 2",
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
    db_session.add_all([product, ticket])
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
    assert 'value="9.99"' in response.text


def test_save_ticket_persists_selected_product(client, db_session):
    product = Product(
        code="P-PROD-SAVE-1",
        description="Save Product",
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
    db_session.add_all([product, ticket])
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
