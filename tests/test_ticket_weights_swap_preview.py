from datetime import datetime
from decimal import Decimal
import re

from app.models import (
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def test_open_ticket_weights_swap_preview_swaps_values(client, db_session):
    ticket = Ticket(
        ticket_no="T-SWAP-PREVIEW-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        gross_kg=12000,
        tare_kg=4000,
        net_kg=8000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/weights/swap-preview",
        data={
            "ticket_id": str(ticket.id),
            "gross_kg": "12000",
            "tare_kg": "4000",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.status_code != 422
    assert 'id="gross_kg"' in response.text
    assert 'name="gross_kg"' in response.text
    assert 'value="4000"' in response.text
    assert 'id="tare_kg"' in response.text
    assert 'name="tare_kg"' in response.text
    assert 'value="12000"' in response.text


def test_open_ticket_edit_swap_button_has_htmx_wiring(client, db_session):
    ticket = Ticket(
        ticket_no="T-SWAP-HTMX-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
    assert 'hx-post="/tickets/weights/swap-preview"' in response.text
    assert 'hx-include="#weights-form"' in response.text
    assert 'hx-target="#weights-block"' in response.text
    assert 'hx-swap="innerHTML"' in response.text


def test_open_ticket_swap_trigger_includes_ticket_id(client, db_session):
    ticket = Ticket(
        ticket_no="T-SWAP-HTMX-2",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
    assert (
        f'hx-vals=\'{{"ticket_id":"{ticket.id}"}}\'' in response.text
        or 'name="ticket_id"' in response.text
    )


def test_open_ticket_weights_swap_preview_requires_weights(client, db_session):
    ticket = Ticket(
        ticket_no="T-SWAP-PREVIEW-2",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/weights/swap-preview",
        data={"ticket_id": str(ticket.id), "gross_kg": "", "tare_kg": ""},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 400
    assert response.status_code != 422
    assert 'id="weights-form"' in response.text
    assert "Gross weight is required." in response.text
    assert "Tare weight is required." in response.text


def test_count_product_swap_preview_returns_400_and_keeps_weights_locked(client, db_session):
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-SWAP-COUNT-1",
        description="Count Product",
        unit_id=unit.id,
        unit_price=Decimal("12.00"),
    )
    ticket = Ticket(
        ticket_no="T-SWAP-PREVIEW-COUNT-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        product_id=product.id,
        gross_kg=12000,
        tare_kg=4000,
        net_kg=8000,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(unit)
    db_session.commit()
    product.unit_id = unit.id
    db_session.add(product)
    db_session.commit()
    ticket.product_id = product.id
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        "/tickets/weights/swap-preview",
        data={
            "ticket_id": str(ticket.id),
            "product_id": str(product.id),
            "gross_kg": "12000",
            "tare_kg": "4000",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 400
    assert response.status_code != 422
    assert "Swap weights not available for COUNT products." in response.text
    assert 'value="12000"' in response.text
    assert 'value="4000"' in response.text
    assert 'id="gross_kg"' in response.text
    assert 'id="tare_kg"' in response.text
    assert 'id="readout_kg"' in response.text
    assert "readonly" in response.text


def test_count_product_edit_view_locks_weights_and_disables_swap(client, db_session):
    unit = Unit(name="CountUiUnit", unit_type="COUNT", is_active=True)
    db_session.add(unit)
    db_session.commit()
    product = Product(
        code="P-COUNT-UI-1",
        description="Count UI Product",
        unit_id=unit.id,
        unit_price=Decimal("8.00"),
    )
    db_session.add(product)
    db_session.commit()
    ticket = Ticket(
        ticket_no="T-COUNT-UI-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert re.search(r'<input[^>]*id="gross_kg"[^>]*readonly', response.text)
    assert re.search(r'<input[^>]*id="tare_kg"[^>]*readonly', response.text)
    assert (
        'disabled title="Swap weights not available for COUNT products."' in response.text
    )


def test_weight_product_edit_view_locks_qty_input(client, db_session):
    unit = Unit(name="WeightUiUnit", unit_type="WEIGHT", is_active=True)
    db_session.add(unit)
    db_session.commit()
    product = Product(
        code="P-WEIGHT-UI-1",
        description="Weight UI Product",
        unit_id=unit.id,
        unit_price=Decimal("8.00"),
    )
    db_session.add(product)
    db_session.commit()
    ticket = Ticket(
        ticket_no="T-WEIGHT-UI-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert re.search(r'<input[^>]*id="qty"[^>]*disabled', response.text)
