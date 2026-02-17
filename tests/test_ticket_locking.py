import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    Customer,
    DirectionEnum,
    EwcCode,
    Invoice,
    Product,
    Ticket,
    TicketStatusEnum,
    TicketVoid,
    TransactionTypeEnum,
    Unit,
    VoidReason,
)
from app.seed import VOID_REASON_TYPE_TICKET, seed_void_reasons


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def test_locked_ticket_update_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-1",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
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
            "datetime": "2026-01-01T10:00",
            "direction": "OUTWARD",
            "transaction_type": "WASTEIN",
        },
    )

    assert response.status_code == 400
    assert "This ticket is complete and cannot be edited." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    assert _status_value(ticket.direction) == DirectionEnum.INWARD.value


def test_void_ticket_update_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-2",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.VOID.value,
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
            "datetime": "2026-01-01T10:00",
            "direction": "OUTWARD",
            "transaction_type": "WASTEIN",
        },
    )

    assert response.status_code == 400
    assert "This ticket is void and cannot be edited." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.VOID.value
    assert _status_value(ticket.direction) == DirectionEnum.INWARD.value


def test_void_ticket_po_update_rejected(client, db_session):
    customer = Customer(
        account_code="C-LOCK-PO-VOID",
        name="Void PO Customer",
        must_have_po=True,
    )
    db_session.add(customer)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-LOCK-PO-VOID",
        datetime=datetime(2026, 1, 2, 10, 0, 0),
        status=TicketStatusEnum.VOID.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        po_number=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/po",
        data={"po_number": "PO-VOID-1"},
    )

    assert response.status_code == 400
    assert "Cannot update PO on a void ticket." in response.text
    assert "PO required for invoicing. Add PO to release this ticket for invoicing." not in response.text
    db_session.refresh(ticket)
    assert ticket.po_number is None


def test_complete_ticket_edit_screen_is_read_only(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-3",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
    assert "This ticket is complete and cannot be edited." in response.text
    assert "<fieldset disabled" in response.text
    assert f'formaction="/tickets/{ticket.id}/po"' in response.text
    assert "Save PO" in response.text
    assert 'id="ticket-po-form"' not in response.text
    assert 'id="po_number"' not in response.text
    assert 'id="po"' in response.text
    assert 'id="po_number_edit"' in response.text
    assert response.text.count('id="po_number_edit"') == 1

    po_input_match = re.search(r'<input[^>]*id="po_number_edit"[^>]*>', response.text)
    assert po_input_match is not None
    assert "disabled" not in po_input_match.group(0).lower()

    save_po_button_match = re.search(
        r'<button[^>]*>\s*Save PO\s*</button>',
        response.text,
    )
    assert save_po_button_match is not None
    assert "disabled" not in save_po_button_match.group(0).lower()


def test_invoiced_complete_ticket_po_update_is_blocked_and_editor_hidden(
    client, db_session
):
    customer = Customer(
        account_code="C-LOCK-PO-INV-1",
        name="PO Invoiced Lock Customer",
    )
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-LOCK-PO-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 4),
        status="DRAFT",
        net_total=Decimal("50.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("50.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-LOCK-PO-INV-1",
        datetime=datetime(2026, 1, 4, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        invoice_id=invoice.id,
        po_number="PO-ORIGINAL-1",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert "PO locked (ticket already invoiced)." in page.text
    assert f'formaction="/tickets/{ticket.id}/po"' not in page.text
    assert 'id="po_number_edit"' not in page.text

    response = client.post(
        f"/tickets/{ticket.id}/po",
        data={"po_number": "PO-SHOULD-NOT-SAVE"},
    )

    assert response.status_code == 400
    assert (
        "Cannot update PO because this ticket has already been invoiced."
        in response.text
    )
    db_session.refresh(ticket)
    assert ticket.po_number == "PO-ORIGINAL-1"


def test_invoiced_ticket_waste_compliance_fields_are_locked_and_updates_blocked(
    client, db_session
):
    customer = Customer(
        account_code="C-LOCK-COMP-INV-1",
        name="Compliance Invoiced Lock Customer",
    )
    unit = Unit(name="Compliance Lock Unit", unit_type="WEIGHT", is_active=True)
    ewc = EwcCode(
        code_6="121212",
        code_display="12 12 12",
        description="Compliance lock EWC",
        hazardous=False,
        active=True,
        source_file="tests",
        imported_at=datetime(2026, 1, 4, 0, 0, 0),
    )
    product = Product(
        code="P-LOCK-COMP-INV-1",
        description="Compliance lock product",
        unit=unit,
        unit_price=Decimal("25.00"),
        ewc_code=ewc,
    )
    db_session.add_all([customer, unit, ewc, product])
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-LOCK-COMP-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 4),
        status="DRAFT",
        net_total=Decimal("50.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("50.00"),
    )
    db_session.add(invoice)
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-LOCK-COMP-INV-1",
        datetime=datetime(2026, 1, 4, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        destination_id=None,
        invoice_id=invoice.id,
        ewc_code_6="121212",
        ewc_code_display="12 12 12",
        ewc_hazardous=False,
        waste_producer_name="Producer Original",
        waste_producer_address="1 Site Road",
        gross_kg=2500,
        tare_kg=1500,
        net_kg=1000,
        unit_price=Decimal("25.00"),
        total=Decimal("25.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert "Cannot update waste compliance because this ticket has already been invoiced." in page.text
    assert re.search(
        r'id="waste-compliance-fieldset"[^>]*disabled',
        page.text,
    )

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-01-04T10:00",
            "direction": "INWARD",
            "transaction_type": "WASTEIN",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "ewc_code": "99 99 99",
            "waste_producer_same_as_customer_present": "1",
            "waste_producer_name": "Changed Producer",
        },
    )

    assert response.status_code == 400
    assert "Cannot update waste compliance because this ticket has already been invoiced." in response.text
    db_session.refresh(ticket)
    assert ticket.ewc_code_6 == "121212"
    assert ticket.waste_producer_name == "Producer Original"


def test_open_ticket_po_update_endpoint_allowed(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-PO-OPEN-1",
        datetime=datetime(2026, 1, 2, 9, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        po_number=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    original_direction = _status_value(ticket.direction)
    original_type = _status_value(ticket.transaction_type)

    response = client.post(
        f"/tickets/{ticket.id}/po",
        data={
            "po_number": "  PO-OPEN-LOCK-1  ",
            "direction": "OUTWARD",
            "transaction_type": "WASTEOUT",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?saved=1")
    db_session.refresh(ticket)
    assert ticket.po_number == "PO-OPEN-LOCK-1"
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value
    assert _status_value(ticket.direction) == original_direction
    assert _status_value(ticket.transaction_type) == original_type


def test_complete_ticket_po_update_endpoint_updates_only_po(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-PO-ONLY-1",
        datetime=datetime(2026, 1, 3, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        gross_kg=12000,
        tare_kg=4000,
        net_kg=8000,
        po_number=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    original = {
        "status": _status_value(ticket.status),
        "direction": _status_value(ticket.direction),
        "transaction_type": _status_value(ticket.transaction_type),
        "gross_kg": ticket.gross_kg,
        "tare_kg": ticket.tare_kg,
        "net_kg": ticket.net_kg,
    }

    response = client.post(
        f"/tickets/{ticket.id}/po",
        data={
            "po_number": "  PO-LOCK-ONLY-1  ",
            "direction": "OUTWARD",
            "gross_kg": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?saved=1")
    db_session.refresh(ticket)
    assert ticket.po_number == "PO-LOCK-ONLY-1"
    assert _status_value(ticket.status) == original["status"]
    assert _status_value(ticket.direction) == original["direction"]
    assert _status_value(ticket.transaction_type) == original["transaction_type"]
    assert ticket.gross_kg == original["gross_kg"]
    assert ticket.tare_kg == original["tare_kg"]
    assert ticket.net_kg == original["net_kg"]


def test_complete_ticket_locked_hides_save_complete_and_shows_void(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-LOCK-3A",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
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
    assert 'name="action" value="save"' not in response.text
    assert 'name="action" value="complete"' not in response.text
    assert '<summary class="frame-header">Void Ticket</summary>' in response.text
    assert '<select id="void_reason_id" name="void_reason_id">' in response.text
    assert '<input type="text" id="void_note" name="void_note"' in response.text
    assert '<button type="submit" class="btn btn--danger">Void Ticket</button>' in response.text
    assert 'id="ticket-status-warnings"' not in response.text
    assert 'id="unsaved-changes-indicator"' not in response.text


def test_void_ticket_locked_hides_all_edit_actions(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-3B",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.VOID.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'name="action" value="save"' not in response.text
    assert 'name="action" value="complete"' not in response.text
    assert '<summary class="frame-header">Void Ticket</summary>' not in response.text
    assert "This ticket is void and cannot be edited." in response.text
    assert 'id="ticket-status-warnings"' not in response.text
    assert 'id="unsaved-changes-indicator"' not in response.text


def test_complete_ticket_void_post_allowed(client, db_session):
    db_session.execute(delete(VoidReason))
    db_session.commit()
    ticket = Ticket(
        ticket_no="T-LOCK-3C",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert "No void reasons configured" not in page.text
    assert "Entered in error" in page.text
    assert "Duplicate ticket" in page.text
    assert "Customer cancelled" in page.text
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
        )
        .order_by(VoidReason.id)
        .limit(1)
    ).scalar_one()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/tickets/{ticket.id}?voided=1")
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.VOID.value
    ticket_void = db_session.execute(
        select(TicketVoid).where(TicketVoid.ticket_id == ticket.id)
    ).scalar_one()
    assert ticket_void.reason_id == reason.id
    follow = client.get(response.headers["location"])
    assert follow.status_code == 200
    assert "Void reason:" in follow.text
    assert (reason.description or reason.code) in follow.text


def test_complete_ticket_void_without_reason_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    ticket = Ticket(
        ticket_no="T-LOCK-3D",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": ""},
    )

    assert response.status_code == 400
    assert "Void reason is required." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value


def test_open_ticket_void_post_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
        )
        .order_by(VoidReason.id)
        .limit(1)
    ).scalar_one()
    ticket = Ticket(
        ticket_no="T-LOCK-3E",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert '<summary class="frame-header">Void Ticket</summary>' in page.text
    assert '<select id="void_reason_id" name="void_reason_id">' in page.text
    assert "Only complete tickets can be voided." in page.text
    assert '<button type="submit" class="btn btn--danger">Void Ticket</button>' not in page.text

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
    )

    assert response.status_code == 400
    assert "Only complete tickets can be voided." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.OPEN.value


def test_invoiced_complete_ticket_void_post_is_blocked(client, db_session):
    seed_void_reasons(db_session)
    reason = db_session.execute(
        select(VoidReason)
        .where(
            VoidReason.is_active.is_(True),
            VoidReason.reason_type == VOID_REASON_TYPE_TICKET,
        )
        .order_by(VoidReason.id)
        .limit(1)
    ).scalar_one()
    customer = Customer(account_code="C-LOCK-INV-1", name="Lock Invoice Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-LOCK-1",
        customer_id=customer.id,
        invoice_date=date(2026, 1, 1),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-LOCK-3F",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        invoice_id=invoice.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    page = client.get(f"/tickets/{ticket.id}")
    assert page.status_code == 200
    assert '<summary class="frame-header">Void Ticket</summary>' in page.text
    assert '<select id="void_reason_id" name="void_reason_id">' in page.text
    assert "Cannot void a ticket that has already been invoiced." in page.text
    assert '<button type="submit" class="btn btn--danger">Void Ticket</button>' not in page.text

    response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
    )

    assert response.status_code == 400
    assert "Cannot void a ticket that has already been invoiced." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    ticket_void = db_session.execute(
        select(TicketVoid).where(TicketVoid.ticket_id == ticket.id)
    ).scalars().first()
    assert ticket_void is None


def test_complete_ticket_weights_render_display_only(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-4",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
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

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert '<input type="number" step="1" id="gross_kg"' not in response.text
    assert '<input type="number" step="1" id="tare_kg"' not in response.text
    assert "Gross kg" in response.text
    assert "Tare kg" in response.text
    assert "Net kg" in response.text
    assert "12000" in response.text
    assert "4000" in response.text
    assert "8000" in response.text


def test_complete_ticket_weights_swap_preview_blocked(client, db_session):
    ticket = Ticket(
        ticket_no="T-LOCK-5",
        datetime=datetime(2026, 1, 1, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
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
        data={"ticket_id": str(ticket.id), "gross_kg": "12000", "tare_kg": "4000"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 400
    assert "This ticket is complete and cannot be edited." in response.text
