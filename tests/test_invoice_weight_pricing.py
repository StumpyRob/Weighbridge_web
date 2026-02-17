from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def test_invoice_weight_product_is_invoiceable_without_qty(client, db_session):
    customer = Customer(account_code="C-INV-WEIGHT-1", name="Invoice Weight Customer")
    unit = Unit(name="tonnes", unit_type="WEIGHT", is_active=True)
    product = Product(
        code="P-INV-WEIGHT-1",
        description="Weight Product",
        unit=unit,
        unit_price=Decimal("20.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-INV-WEIGHT-1",
        datetime=datetime(2026, 2, 14, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        gross_kg=Decimal("6200"),
        tare_kg=Decimal("1200"),
        net_kg=Decimal("5000"),
        qty=None,
        unit_price=Decimal("20.00"),
        total=Decimal("100.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_unit_price=Decimal("20.00"),
        pricing_net_kg_snapshot=Decimal("5000"),
        pricing_billable_qty_snapshot=Decimal("5.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

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
    assert "Missing quantity/price" not in preview.text

    confirm = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303

    line = db_session.execute(
        select(InvoiceLine).where(InvoiceLine.ticket_id == ticket.id)
    ).scalar_one()
    assert float(line.quantity) == 5.0
    assert float(line.unit_price) == 20.0
    assert float(line.net) == 100.0
    assert float(line.gross) == 100.0

    invoice = db_session.execute(
        select(Invoice).where(Invoice.id == line.invoice_id)
    ).scalar_one()
    assert float(invoice.net_total) == 100.0
    assert float(invoice.gross_total) == 100.0


def test_invoice_count_product_still_requires_qty(client, db_session):
    customer = Customer(account_code="C-INV-COUNT-1", name="Invoice Count Customer")
    unit = Unit(name="Each", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-INV-COUNT-1",
        description="Count Product",
        unit=unit,
        unit_price=Decimal("15.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-INV-COUNT-1",
        datetime=datetime(2026, 2, 14, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=None,
        unit_price=Decimal("15.00"),
        total=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

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
    assert "Missing quantity/price" in preview.text

    before_count = len(db_session.execute(select(Invoice)).scalars().all())
    confirm = client.post(
        "/invoices/generate/confirm",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert confirm.status_code == 200
    assert "No invoiceable tickets found." in confirm.text
    after_count = len(db_session.execute(select(Invoice)).scalars().all())
    assert after_count == before_count


def test_invoice_weight_exclusion_reasons_are_specific(client, db_session):
    customer = Customer(account_code="C-INV-WEIGHT-2", name="Invoice Weight Reasons")
    unit = Unit(name="kg", unit_type="WEIGHT", is_active=True)
    product = Product(
        code="P-INV-WEIGHT-2",
        description="Weight Reason Product",
        unit=unit,
        unit_price=Decimal("5.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    missing_net = Ticket(
        ticket_no="T-INV-WEIGHT-NET",
        datetime=datetime(2026, 2, 14, 11, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        net_kg=None,
        qty=None,
        unit_price=Decimal("5.00"),
        total=None,
        dont_invoice=False,
        paid=False,
    )
    missing_price = Ticket(
        ticket_no="T-INV-WEIGHT-PRICE",
        datetime=datetime(2026, 2, 14, 12, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        customer_id=customer.id,
        product_id=product.id,
        net_kg=Decimal("1000"),
        qty=None,
        unit_price=None,
        total=None,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([missing_net, missing_price])
    db_session.commit()

    preview = client.post(
        "/invoices/generate",
        data={
            "customer_id": str(customer.id),
            "date_from": "01/02/2026",
            "date_to": "28/02/2026",
        },
    )

    assert preview.status_code == 200
    assert "Missing net weight" in preview.text
    assert "Missing price" in preview.text
