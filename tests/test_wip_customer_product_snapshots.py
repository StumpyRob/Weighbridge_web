from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.config import Settings
from app.models import (
    Customer,
    Destination,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def test_customer_wip_fields_persist_on_create_and_edit(client, db_session):
    create = client.post(
        "/customers/new",
        data={
            "account_code": "C-WIP-1",
            "name": "WIP Customer",
            "vat_number": "GB123456789",
            "credit_limit_pounds": "123.45",
            "is_cash_account": "on",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    customer = db_session.execute(
        select(Customer).where(Customer.account_code == "C-WIP-1")
    ).scalar_one()
    assert customer.vat_number == "GB123456789"
    assert customer.credit_limit_pence == 12345
    assert customer.is_cash_account is True

    update = client.post(
        f"/customers/{customer.id}",
        data={
            "account_code": "C-WIP-1",
            "name": "WIP Customer Updated",
            "vat_number": "GB000000001",
            "credit_limit_pounds": "0.50",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303

    db_session.refresh(customer)
    assert customer.name == "WIP Customer Updated"
    assert customer.vat_number == "GB000000001"
    assert customer.credit_limit_pence == 50
    assert customer.is_cash_account is False


def test_product_wip_flags_persist_on_create_and_edit(client, db_session):
    tax_rate = TaxRate(
        code="WIP VAT 20",
        description="WIP tax",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.commit()

    create = client.post(
        "/products/new",
        data={
            "code": "P-WIP-1",
            "description": "WIP Product",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "20.00",
            "final_disposal_wip": "on",
            "used_on_site_wip": "",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    product = db_session.execute(
        select(Product).where(Product.code == "P-WIP-1")
    ).scalar_one()
    assert product.final_disposal_wip is True
    assert product.used_on_site_wip is False

    update = client.post(
        f"/products/{product.id}",
        data={
            "code": "P-WIP-1",
            "description": "WIP Product Updated",
            "sale_type": "WEIGHT",
            "product_type": "sale",
            "tax_rate_id": str(tax_rate.id),
            "unit_price": "21.00",
            "used_on_site_wip": "on",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303

    db_session.refresh(product)
    assert product.description == "WIP Product Updated"
    assert product.final_disposal_wip is False
    assert product.used_on_site_wip is True


def test_ticket_complete_stores_wip_snapshot_values(client, db_session):
    customer = Customer(
        account_code="C-WIP-SNAP-1",
        name="Snapshot Customer",
        vat_number="GBSNAP001",
        credit_limit_pence=98765,
        is_cash_account=True,
    )
    unit = Unit(name="WIP Snapshot Count Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-WIP-SNAP-1",
        description="Snapshot Product",
        unit=unit,
        unit_price=Decimal("5.00"),
        final_disposal_wip=True,
        used_on_site_wip=False,
    )
    destination = Destination(name="WIP Snapshot Destination")
    db_session.add_all([customer, unit, product])
    db_session.add(destination)
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-WIP-SNAP-1",
        datetime=datetime(2026, 2, 22, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    complete = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-22T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "destination_id": str(destination.id),
            "qty": "1",
            "unit_price": "5.00",
            "po_number": "",
        },
        follow_redirects=False,
    )
    assert complete.status_code == 303

    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.wip_snapshot_json == {
        "customer": {
            "vat_number": "GBSNAP001",
            "is_cash_account": True,
            "credit_limit_pence": 98765,
        },
        "product": {
            "final_disposal_wip": True,
            "used_on_site_wip": False,
        },
    }

    customer.vat_number = "GBCHANGED"
    customer.credit_limit_pence = 1
    customer.is_cash_account = False
    product.final_disposal_wip = False
    product.used_on_site_wip = True
    db_session.commit()

    db_session.refresh(ticket)
    assert ticket.wip_snapshot_json["customer"]["vat_number"] == "GBSNAP001"
    assert ticket.wip_snapshot_json["customer"]["credit_limit_pence"] == 98765
    assert ticket.wip_snapshot_json["customer"]["is_cash_account"] is True
    assert ticket.wip_snapshot_json["product"]["final_disposal_wip"] is True
    assert ticket.wip_snapshot_json["product"]["used_on_site_wip"] is False


def test_invoice_generation_stores_customer_and_product_wip_snapshots(client, db_session):
    customer = Customer(
        account_code="C-WIP-INV-1",
        name="Invoice Snapshot Customer",
        vat_number="GBINV001",
        credit_limit_pence=43210,
        is_cash_account=False,
    )
    unit = Unit(name="WIP Invoice Count Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-WIP-INV-1",
        description="Invoice Snapshot Product",
        unit=unit,
        unit_price=Decimal("10.00"),
        final_disposal_wip=False,
        used_on_site_wip=True,
    )
    tax_rate = TaxRate(
        code="WIP VAT 0",
        description="WIP VAT Zero",
        rate_percent=Decimal("0.00"),
        is_active=True,
    )
    db_session.add_all([customer, unit, product, tax_rate])
    db_session.flush()
    product.tax_rate_id = tax_rate.id

    ticket = Ticket(
        ticket_no="T-WIP-INV-1",
        datetime=datetime(2026, 2, 22, 12, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=2,
        unit_price=Decimal("10.00"),
        total=Decimal("20.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

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

    invoice = db_session.execute(select(Invoice).order_by(Invoice.id.desc())).scalars().first()
    assert invoice is not None
    assert invoice.customer_snapshot_json == {
        "vat_number": "GBINV001",
        "is_cash_account": False,
        "credit_limit_pence": 43210,
    }

    line = db_session.execute(
        select(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice.id)
        .order_by(InvoiceLine.id.asc())
    ).scalars().first()
    assert line is not None
    assert line.product_snapshot_json == {
        "final_disposal_wip": False,
        "used_on_site_wip": True,
    }

    customer.vat_number = "GBINVCHANGED"
    customer.credit_limit_pence = 999
    customer.is_cash_account = True
    product.final_disposal_wip = True
    product.used_on_site_wip = False
    db_session.commit()

    db_session.refresh(invoice)
    db_session.refresh(line)
    assert invoice.customer_snapshot_json["vat_number"] == "GBINV001"
    assert invoice.customer_snapshot_json["credit_limit_pence"] == 43210
    assert invoice.customer_snapshot_json["is_cash_account"] is False
    assert line.product_snapshot_json["final_disposal_wip"] is False
    assert line.product_snapshot_json["used_on_site_wip"] is True


def test_wip_feature_flags_default_to_off():
    settings = Settings(
        database_url="sqlite:///./test-settings.db",
        secret_key="test-secret",
    )

    assert settings.enable_credit_limit_enforcement is False
    assert settings.enable_vat_calculation is False
    assert settings.enable_cash_account_rules is False
