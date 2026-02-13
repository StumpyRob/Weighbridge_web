from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    VoidReason,
)


def _status_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def test_void_ticket_is_excluded_from_invoice_preview(client, db_session):
    customer = Customer(account_code="C-INV-VOID-EX-1", name="Invoice Void Exclusion")
    unit = Unit(name="Invoice Void Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-INV-VOID-EX-1",
        description="Invoice Void Product",
        unit=unit,
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-INV-VOID-EX-1",
        datetime=datetime(2026, 2, 12, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product=product,
        qty=1,
        unit_price=Decimal("10.00"),
        total=Decimal("10.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    preview_before = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )
    assert preview_before.status_code == 200
    assert ticket.ticket_no in preview_before.text

    # Ensure default reasons exist in clean DB and void the ticket.
    client.get(f"/tickets/{ticket.id}")
    reason = db_session.execute(
        select(VoidReason).where(VoidReason.is_active.is_(True)).limit(1)
    ).scalar_one()
    void_response = client.post(
        f"/tickets/{ticket.id}",
        data={"action": "void", "void_reason_id": str(reason.id)},
        follow_redirects=False,
    )
    assert void_response.status_code == 303
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.VOID.value

    preview_after = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )
    assert preview_after.status_code == 200
    assert ticket.ticket_no not in preview_after.text
