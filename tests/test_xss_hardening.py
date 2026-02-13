from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TicketVoid,
    TransactionTypeEnum,
    Unit,
    VoidReason,
)


def _status_value(value):
    return value.value if hasattr(value, "value") else str(value)


def test_customer_name_rejects_script_payload(client, db_session):
    response = client.post(
        "/customers/new",
        data={
            "account_code": "C-XSS-1",
            "name": "<script>alert(1)</script>",
        },
    )

    assert response.status_code == 400
    assert "HTML is not allowed." in response.text
    customer = db_session.execute(
        select(Customer).where(Customer.account_code == "C-XSS-1")
    ).scalars().first()
    assert customer is None

    list_response = client.get("/customers")
    assert list_response.status_code == 200
    assert "<script>alert(1)</script>" not in list_response.text


def test_product_description_rejects_script_payload(client, db_session):
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="TEST VAT",
        description="Test VAT",
        rate_percent=Decimal("0.200"),
        is_active=True,
    )
    db_session.add_all([unit, tax_rate])
    db_session.commit()

    response = client.post(
        "/products/new",
        data={
            "code": "P-XSS-1",
            "description": "<script>alert(1)</script>",
            "sale_type": "COUNT",
            "unit_id": str(unit.id),
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "10.00",
            "ewc_code_id": "",
            "ewc_code_label": "",
            "default_destination_id": "",
            "is_hazardous": "",
        },
    )

    assert response.status_code == 400
    assert "HTML is not allowed." in response.text
    product = db_session.execute(
        select(Product).where(Product.code == "P-XSS-1")
    ).scalars().first()
    assert product is None

    list_response = client.get("/products")
    assert list_response.status_code == 200
    assert "<script>alert(1)</script>" not in list_response.text


def test_ticket_void_note_rejects_script_payload(client, db_session):
    reason = VoidReason(code="XSS-TEST", description="XSS test", is_active=True)
    ticket = Ticket(
        ticket_no="T-XSS-1",
        datetime=datetime(2026, 1, 5, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([reason, ticket])
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "void",
            "void_reason_id": str(reason.id),
            "void_note": "<script>alert(1)</script>",
        },
    )

    assert response.status_code == 400
    assert "HTML is not allowed." in response.text
    db_session.refresh(ticket)
    assert _status_value(ticket.status) == TicketStatusEnum.COMPLETE.value
    ticket_void = db_session.execute(
        select(TicketVoid).where(TicketVoid.ticket_id == ticket.id)
    ).scalars().first()
    assert ticket_void is None

    detail_response = client.get(f"/tickets/{ticket.id}")
    assert detail_response.status_code == 200
    assert "<script>alert(1)</script>" not in detail_response.text
