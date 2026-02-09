from datetime import datetime

from decimal import Decimal

from app.models import (
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def test_ticket_edit_includes_live_recalc_hook(client, db_session):
    ticket = Ticket(
        ticket_no="T-LIVE-CALC-1",
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
    assert "function recalculateTicketValues()" in response.text
    assert 'const netInput = document.getElementById("net_kg")' in response.text
    assert 'netInput.value = net === null ? "" : String(Math.round(net));' in response.text
    assert 'target.id === "gross_kg"' in response.text
    assert 'target.id === "tare_kg"' in response.text
    assert 'target.id === "qty"' in response.text
    assert 'target.id === "unit_price"' in response.text
    assert 'target.id === "product_id"' in response.text
    assert 'event.target && event.target.id === "weights-block"' in response.text


def test_ticket_edit_includes_live_product_mode_lock_hooks(client, db_session):
    count_unit = Unit(name="Each-Live-Hook", unit_type="COUNT", is_active=True)
    ticket = Ticket(
        ticket_no="T-LIVE-CALC-2",
        datetime=datetime(2026, 1, 1, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(count_unit)
    db_session.commit()
    product = Product(
        code="P-LIVE-HOOK-COUNT-1",
        description="Live Hook Product",
        unit_id=count_unit.id,
        unit_price=Decimal("5.00"),
    )
    db_session.add(product)
    db_session.commit()
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "function applySelectedProductMeta()" in response.text
    assert "function bindProductMetaSync()" in response.text
    assert 'data-unit-type="COUNT"' in response.text
    assert 'data-unit-name="Each-Live-Hook"' in response.text
    assert 'const swapButton = document.getElementById("swap-weights-button")' in response.text
    assert "qtyInput.readOnly = true;" in response.text
    assert "grossInput.readOnly = true;" in response.text
