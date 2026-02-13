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
    assert 'netInput.value = unitType === "WEIGHT" && net !== null ? String(Math.round(net)) : "";' in response.text
    assert 'target.id === "gross_kg"' in response.text
    assert 'target.id === "tare_kg"' in response.text
    assert 'target.id === "qty"' in response.text
    assert 'target.id === "unit_price"' in response.text
    assert 'target.id === "product_id"' in response.text
    assert 'event.target.id === "weights-block"' in response.text


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
    assert "function applyTicketRules()" in response.text
    assert "function applyPricingRules()" in response.text
    assert "function bindProductMetaSync()" in response.text
    assert "function bindTransactionTypeSync()" in response.text
    assert "function bindDirectionSync()" in response.text
    assert "function scheduleApplyTicketRules()" in response.text
    assert "function isTicketRulesHtmxRequest(event)" in response.text
    assert "function setTicketRulesBusy(isBusy)" in response.text
    assert 'document.addEventListener("htmx:beforeRequest"' in response.text
    assert 'document.addEventListener("htmx:afterSwap"' in response.text
    assert 'document.addEventListener("htmx:afterSettle"' in response.text
    assert 'data-unit-type="COUNT"' in response.text
    assert 'data-unit-name="Each-Live-Hook"' in response.text
    assert 'const swapButton = document.getElementById("swap-weights-button")' in response.text
    assert 'grossInput.value = "";' in response.text
    assert 'tareInput.value = "";' in response.text
    assert "qtyInput.disabled = true;" in response.text
    assert "grossInput.readOnly = true;" in response.text
    assert 'requestPath.indexOf("/tickets/product-defaults") !== -1' in response.text
    assert 'requestPath.indexOf("/tickets/product-options") !== -1' in response.text


def test_ticket_edit_registers_unsaved_changes_beforeunload_guard(client, db_session):
    ticket = Ticket(
        ticket_no="T-LIVE-CALC-3",
        datetime=datetime(2026, 1, 1, 12, 0, 0),
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
    assert 'id="unsaved-changes-indicator"' in response.text
    assert "function bindUnsavedChangesGuard()" in response.text
    assert 'let dirty = false;' in response.text
    assert 'ticketForm.addEventListener("input", markDirty);' in response.text
    assert 'ticketForm.addEventListener("change", markDirty);' in response.text
    assert 'window.addEventListener("beforeunload", function (event)' in response.text


def test_ticket_edit_locked_skips_js_initialization(client, db_session):
    ticket = Ticket(
        ticket_no="T-LIVE-CALC-LOCK-1",
        datetime=datetime(2026, 1, 1, 13, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "const isLocked = true;" in response.text
    assert "if (isLocked) {" in response.text
    assert "return;" in response.text
    assert response.text.find("if (isLocked) {") < response.text.find("bindCalculations();")
    assert 'id="ticket-status-warnings"' not in response.text
    assert 'id="unsaved-changes-indicator"' not in response.text
