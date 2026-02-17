from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _make_invoiceable_ticket(
    db_session,
    *,
    customer_id: int,
    ticket_no: str,
    dt: datetime,
    po_number: str | None = None,
) -> Ticket:
    unit = Unit(name=f"Invoice Rule Unit {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"P-INV-RULE-{ticket_no}",
        description=f"Invoice Rule Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("50.00"),
    )
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=dt,
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer_id,
        product=product,
        qty=1,
        unit_price=Decimal("50.00"),
        total=Decimal("50.00"),
        po_number=po_number,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()
    return ticket


def _make_open_sale_ticket(
    db_session,
    *,
    customer_id: int,
    ticket_no: str,
    dt: datetime,
    po_number: str | None = None,
) -> Ticket:
    unit = Unit(name=f"Open Invoice Rule Unit {ticket_no}", unit_type="COUNT", is_active=True)
    product = Product(
        code=f"P-OPEN-INV-RULE-{ticket_no}",
        description=f"Open Invoice Rule Product {ticket_no}",
        unit=unit,
        unit_price=Decimal("50.00"),
    )
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=dt,
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer_id,
        product=product,
        po_number=po_number,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([unit, product, ticket])
    db_session.commit()
    return ticket


def test_do_not_invoice_customer_is_excluded_then_becomes_eligible_when_turned_off(
    client, db_session
):
    customer = Customer(
        account_code="C-DNI-1",
        name="Do Not Invoice Customer",
        do_not_invoice=True,
    )
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-DNI-1",
        dt=datetime(2026, 2, 10, 9, 0, 0),
    )

    blocked = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert blocked.status_code == 200
    assert ticket.ticket_no not in blocked.text
    assert "No tickets found." in blocked.text

    customer.do_not_invoice = False
    db_session.commit()

    allowed = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert allowed.status_code == 200
    assert ticket.ticket_no in allowed.text


def test_must_have_po_customer_excludes_missing_po_then_allows_with_po(client, db_session):
    customer = Customer(
        account_code="C-PO-1",
        name="PO Required Customer",
        must_have_po=True,
    )
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-PO-1",
        dt=datetime(2026, 2, 11, 10, 0, 0),
        po_number=None,
    )

    blocked = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert blocked.status_code == 200
    assert ticket.ticket_no in blocked.text
    assert "Missing PO" in blocked.text
    assert "tickets excluded because PO missing." not in blocked.text

    ticket.po_number = "PO-12345"
    db_session.commit()

    allowed = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert allowed.status_code == 200
    assert ticket.ticket_no in allowed.text


def test_must_have_po_customer_allows_complete_without_po_but_excludes_from_invoice_preview(
    client, db_session
):
    customer = Customer(
        account_code="C-PO-COMPLETE-WITHOUT-1",
        name="PO Complete Without Customer",
        must_have_po=True,
    )
    db_session.add(customer)
    db_session.commit()
    ticket = _make_open_sale_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-PO-COMPLETE-WITHOUT-1",
        dt=datetime(2026, 2, 11, 10, 20, 0),
        po_number=None,
    )

    complete = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-11T10:20",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(ticket.product_id),
            "qty": "1",
            "unit_price": "50.00",
            "po_number": "",
        },
        follow_redirects=False,
    )

    assert complete.status_code == 303
    assert complete.headers["location"].endswith(f"/tickets/{ticket.id}?completed=1")
    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.po_number is None

    preview = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert preview.status_code == 200
    assert ticket.ticket_no in preview.text
    assert "Missing PO" in preview.text


def test_complete_ticket_po_can_be_added_later_without_unlocking(client, db_session):
    customer = Customer(
        account_code="C-PO-LATE-1",
        name="PO Late Entry Customer",
        must_have_po=True,
    )
    db_session.add(customer)
    db_session.commit()
    ticket = _make_open_sale_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-PO-LATE-1",
        dt=datetime(2026, 2, 11, 11, 0, 0),
        po_number=None,
    )
    complete = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-11T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(ticket.product_id),
            "qty": "1",
            "unit_price": "50.00",
            "po_number": "",
        },
        follow_redirects=False,
    )
    assert complete.status_code == 303
    assert complete.headers["location"].endswith(f"/tickets/{ticket.id}?completed=1")
    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.po_number is None

    original = {
        "datetime": ticket.datetime,
        "status": ticket.status,
        "direction": ticket.direction,
        "transaction_type": ticket.transaction_type,
        "customer_id": ticket.customer_id,
        "qty": ticket.qty,
        "unit_price": ticket.unit_price,
        "total": ticket.total,
        "updated_at": ticket.updated_at,
    }

    blocked = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert blocked.status_code == 200
    assert ticket.ticket_no in blocked.text
    assert "Missing PO" in blocked.text
    assert f"/tickets/{ticket.id}#po" in blocked.text

    ticket_page = client.get(f"/tickets/{ticket.id}")
    assert ticket_page.status_code == 200
    assert "This ticket is complete and cannot be edited." in ticket_page.text
    assert f'formaction="/tickets/{ticket.id}/po"' in ticket_page.text
    assert 'id="po"' in ticket_page.text
    po_banner_text = "PO required for invoicing. Add PO to release this ticket for invoicing."
    assert po_banner_text in ticket_page.text
    assert ticket_page.text.count(po_banner_text) == 1

    po_update = client.post(
        f"/tickets/{ticket.id}/po",
        data={
            "po_number": "  PO-LATE-123  ",
            "direction": "OUTWARD",
            "transaction_type": "WASTEOUT",
        },
        follow_redirects=False,
    )
    assert po_update.status_code == 303
    assert po_update.headers["location"].endswith(f"/tickets/{ticket.id}?saved=1")

    db_session.refresh(ticket)
    assert ticket.po_number == "PO-LATE-123"
    assert ticket.datetime == original["datetime"]
    assert ticket.status == original["status"]
    assert ticket.direction == original["direction"]
    assert ticket.transaction_type == original["transaction_type"]
    assert ticket.customer_id == original["customer_id"]
    assert ticket.qty == original["qty"]
    assert ticket.unit_price == original["unit_price"]
    assert ticket.total == original["total"]
    if original["updated_at"] is not None and ticket.updated_at is not None:
        assert ticket.updated_at >= original["updated_at"]

    allowed = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert allowed.status_code == 200
    assert ticket.ticket_no in allowed.text
    assert "Missing PO" not in allowed.text


def test_po_update_blocked_after_invoice_confirm(client, db_session):
    customer = Customer(
        account_code="C-PO-LOCKED-AFTER-INV-1",
        name="PO Locked After Invoice Customer",
    )
    db_session.add(customer)
    db_session.commit()
    ticket = _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-PO-LOCKED-AFTER-INV-1",
        dt=datetime(2026, 2, 11, 12, 0, 0),
        po_number="PO-LOCK-BEFORE-INV",
    )

    create = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    db_session.refresh(ticket)
    assert ticket.invoice_id is not None
    assert ticket.po_number == "PO-LOCK-BEFORE-INV"

    ticket_page = client.get(f"/tickets/{ticket.id}")
    assert ticket_page.status_code == 200
    assert "PO locked (ticket already invoiced)." in ticket_page.text
    assert f'formaction="/tickets/{ticket.id}/po"' not in ticket_page.text
    assert 'id="po_number_edit"' not in ticket_page.text

    po_update = client.post(
        f"/tickets/{ticket.id}/po",
        data={"po_number": "PO-SHOULD-NOT-SAVE"},
    )
    assert po_update.status_code == 400
    assert (
        "Cannot update PO because this ticket has already been invoiced."
        in po_update.text
    )

    db_session.refresh(ticket)
    assert ticket.po_number == "PO-LOCK-BEFORE-INV"


def test_ticket_edit_shows_customer_invoiceability_warnings(client, db_session):
    customer = Customer(
        account_code="C-WARN-1",
        name="Warning Customer",
        do_not_invoice=True,
        must_have_po=True,
    )
    db_session.add(customer)
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-WARN-1",
        datetime=datetime(2026, 2, 12, 8, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        po_number=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert (
        "Customer is marked Do not invoice - this ticket will never be invoiceable."
        in response.text
    )
    assert (
        "PO required for invoicing. Add PO to release this ticket for invoicing."
        not in response.text
    )


def test_invoice_due_date_uses_customer_payment_terms_days(client, db_session):
    customer = Customer(
        account_code="C-TERM-30",
        name="Terms 30 Customer",
        payment_terms_days=30,
    )
    db_session.add(customer)
    db_session.commit()
    _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-TERM-30",
        dt=datetime(2026, 2, 13, 10, 0, 0),
        po_number="PO-TERM-30",
    )

    response = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    invoice = db_session.execute(
        select(Invoice).order_by(Invoice.id.desc()).limit(1)
    ).scalar_one()
    assert invoice.customer_id == customer.id
    assert invoice.due_date == invoice.invoice_date + timedelta(days=30)

    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Due" in detail.text
    assert invoice.due_date.strftime("%d/%m/%Y") in detail.text


def test_invoice_due_date_is_null_when_customer_terms_not_set(client, db_session):
    customer = Customer(account_code="C-TERM-NULL", name="No Terms Customer")
    db_session.add(customer)
    db_session.commit()
    _make_invoiceable_ticket(
        db_session,
        customer_id=customer.id,
        ticket_no="T-TERM-NULL",
        dt=datetime(2026, 2, 13, 11, 0, 0),
        po_number="PO-TERM-NULL",
    )

    response = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    invoice = db_session.execute(
        select(Invoice).order_by(Invoice.id.desc()).limit(1)
    ).scalar_one()
    assert invoice.customer_id == customer.id
    assert invoice.due_date is None


def test_customer_edit_persists_invoiceability_flags_and_payment_terms(client, db_session):
    customer = Customer(account_code="C-CUST-FLAG", name="Customer Flags")
    db_session.add(customer)
    db_session.commit()

    enable = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": customer.account_code,
            "name": customer.name,
            "invoice_frequency": "MONTHLY",
            "do_not_invoice": "on",
            "must_have_po": "on",
            "payment_terms_days": "14",
        },
        follow_redirects=False,
    )

    assert enable.status_code == 303
    db_session.refresh(customer)
    assert customer.do_not_invoice is True
    assert customer.must_have_po is True
    assert customer.invoice_frequency == "MONTHLY"
    assert customer.payment_terms_days == 14

    disable = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": customer.account_code,
            "name": customer.name,
            "invoice_frequency": "",
            "payment_terms_days": "",
        },
        follow_redirects=False,
    )

    assert disable.status_code == 303
    db_session.refresh(customer)
    assert customer.do_not_invoice is False
    assert customer.must_have_po is False
    assert customer.invoice_frequency is None
    assert customer.payment_terms_days is None
