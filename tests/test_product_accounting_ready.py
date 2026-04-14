from datetime import datetime
from decimal import Decimal
import re

from sqlalchemy import select

from app.models import (
    Customer,
    CustomerProductPrice,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    Product,
    ProductGroup,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)
from app.services.pricing import product_effective_nominal_code


def _input_value(html: str, input_id: str) -> str:
    match = re.search(rf'<input[^>]*id="{input_id}"[^>]*value="([^"]*)"', html)
    assert match is not None
    return match.group(1)


def test_product_group_crud_and_deactivate_guard(client, db_session):
    create = client.post(
        "/products/groups/new",
        data={
            "name": "Aggregates",
            "description": "Aggregate materials",
            "nominal_code_default": "4000",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"].endswith("/products/groups?saved=1")

    group = db_session.execute(
        select(ProductGroup).where(ProductGroup.name == "Aggregates")
    ).scalar_one()
    assert group.nominal_code_default == "4000"

    duplicate = client.post(
        "/products/groups/new",
        data={
            "name": "Aggregates",
            "description": "",
            "nominal_code_default": "",
        },
    )
    assert duplicate.status_code == 400
    assert "Name already exists." in duplicate.text

    unit = Unit(name="Group Guard Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-GRP-GUARD-1",
        description="Group Guard Product",
        unit=unit,
        unit_price=Decimal("10.00"),
        group_id=group.id,
    )
    db_session.add_all([unit, product])
    db_session.commit()

    blocked = client.post(
        f"/products/groups/{group.id}/deactivate",
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert "error=Cannot+deactivate:+in+use+by+products." in blocked.headers["location"]

    group_page = client.get(blocked.headers["location"])
    assert group_page.status_code == 200
    assert "Cannot deactivate: in use by products." in group_page.text

    db_session.refresh(group)
    assert group.is_active is True


def test_product_nominal_code_inheritance_helper_and_edit_ui(client, db_session):
    group = ProductGroup(
        code="NOMINAL-GRP-1",
        name="Nominal Group",
        nominal_code_default="4000",
        is_active=True,
    )
    unit = Unit(name="Nominal Unit", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="Nominal VAT",
        description="Nominal VAT",
        rate_percent=Decimal("0.200"),
        is_active=True,
    )
    product = Product(
        code="P-NOMINAL-1",
        description="Nominal Product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("15.00"),
        product_group=group,
        nominal_code=None,
    )
    db_session.add_all([group, unit, tax_rate, product])
    db_session.commit()

    assert product_effective_nominal_code(product) == "4000"
    product.nominal_code = "4100"
    db_session.commit()
    assert product_effective_nominal_code(product) == "4100"

    response = client.get(f"/products/{product.id}")
    assert response.status_code == 200
    assert 'id="group_id"' in response.text
    assert 'id="nominal_code"' in response.text
    assert 'value="4100"' in response.text
    assert "Cash price" not in response.text
    assert "Min price" not in response.text
    assert "Max price" not in response.text
    assert "Excess trigger" not in response.text
    assert "Excess price" not in response.text


def test_product_edit_shows_selected_inactive_group(client, db_session):
    inactive_group = ProductGroup(
        code="INACTIVE-GRP-1",
        name="Inactive Group",
        nominal_code_default="5000",
        is_active=False,
    )
    unit = Unit(name="Inactive Group Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-INACTIVE-GRP-1",
        description="Inactive Group Product",
        unit=unit,
        unit_price=Decimal("8.00"),
    )
    db_session.add_all([inactive_group, unit])
    db_session.flush()
    product.group_id = inactive_group.id
    db_session.add(product)
    db_session.commit()

    response = client.get(f"/products/{product.id}")
    assert response.status_code == 200
    assert "Inactive Group" in response.text
    assert "(inactive)" in response.text


def test_customer_product_price_override_applies_to_product_defaults_and_ticket_save(
    client, db_session
):
    customer = Customer(account_code="C-OVR-1", name="Override Customer")
    other_customer = Customer(account_code="C-OVR-2", name="Fallback Customer")
    unit = Unit(name="Override Count Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-OVR-1",
        description="Override Product",
        unit=unit,
        unit_price=Decimal("12.00"),
    )
    db_session.add_all([customer, other_customer, unit, product])
    db_session.flush()
    ticket = Ticket(
        ticket_no="T-OVR-1",
        datetime=datetime(2026, 2, 16, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        dont_invoice=False,
        paid=False,
    )
    override = CustomerProductPrice(
        customer_id=customer.id,
        product_id=product.id,
        unit_price=Decimal("9.50"),
        is_active=True,
    )
    db_session.add_all([ticket, override])
    db_session.commit()

    defaults = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(product.id),
            "ticket_id": str(ticket.id),
            "customer_id": str(customer.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "",
        },
    )
    assert defaults.status_code == 200
    assert _input_value(defaults.text, "unit_price") == "9.50"
    assert "Using customer price override: &pound;9.50" in defaults.text

    no_override_defaults = client.get(
        "/tickets/product-defaults",
        params={
            "product_id": str(product.id),
            "ticket_id": str(ticket.id),
            "customer_id": str(other_customer.id),
            "transaction_type": "SALE",
            "gross_kg": "",
            "tare_kg": "",
            "net_kg": "",
            "readout_kg": "",
            "qty": "",
            "unit_price": "",
        },
    )
    assert no_override_defaults.status_code == 200
    assert _input_value(no_override_defaults.text, "unit_price") == "12.00"
    assert "Using customer price override" not in no_override_defaults.text

    save = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "save",
            "datetime": "2026-02-16T10:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "2",
            "unit_price": "",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303

    db_session.refresh(ticket)
    assert ticket.unit_price == Decimal("9.50")
    assert ticket.total == Decimal("19.00")


def test_customer_override_pricing_flows_into_generated_invoice(client, db_session):
    customer = Customer(account_code="C-OVR-INV-1", name="Override Invoice Customer")
    unit = Unit(name="Override Invoice Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-OVR-INV-1",
        description="Override Invoice Product",
        unit=unit,
        unit_price=Decimal("12.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-OVR-INV-1",
        datetime=datetime(2026, 2, 16, 14, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        dont_invoice=False,
        paid=False,
    )
    override = CustomerProductPrice(
        customer_id=customer.id,
        product_id=product.id,
        unit_price=Decimal("9.50"),
        is_active=True,
    )
    db_session.add_all([ticket, override])
    db_session.commit()

    complete = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-16T14:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "qty": "3",
            "unit_price": "",
        },
        follow_redirects=False,
    )
    assert complete.status_code == 303
    assert complete.headers["location"].endswith(f"/tickets/{ticket.id}?completed=1")

    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.unit_price == Decimal("9.50")
    assert ticket.total == Decimal("28.50")

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
    assert float(line.quantity) == 3.0
    assert float(line.unit_price) == 9.5
    assert float(line.net) == 28.5

    invoice = db_session.execute(
        select(Invoice).where(Invoice.id == line.invoice_id)
    ).scalar_one()
    assert float(invoice.net_total) == 28.5


def test_weight_ticket_complete_uses_customer_override_when_unit_price_blank(
    client, db_session
):
    customer = Customer(account_code="C-OVR-WEIGHT-1", name="Weight Override Customer")
    weight_unit = Unit(name="tonnes", unit_type="WEIGHT", is_active=True)
    product = Product(
        code="P-OVR-WEIGHT-1",
        description="Weight Override Product",
        unit=weight_unit,
        unit_price=Decimal("25.00"),
    )
    ticket = Ticket(
        ticket_no="T-OVR-WEIGHT-1",
        datetime=datetime(2026, 2, 16, 11, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([customer, weight_unit, product, ticket])
    db_session.flush()
    override = CustomerProductPrice(
        customer_id=customer.id,
        product_id=product.id,
        unit_price=Decimal("30.00"),
        is_active=True,
    )
    db_session.add(override)
    db_session.commit()

    complete = client.post(
        f"/tickets/{ticket.id}",
        data={
            "action": "complete",
            "datetime": "2026-02-16T11:00",
            "direction": "INWARD",
            "transaction_type": "SALE",
            "customer_id": str(customer.id),
            "product_id": str(product.id),
            "gross_kg": "12000",
            "tare_kg": "2000",
            "qty": "",
            "unit_price": "",
        },
        follow_redirects=False,
    )
    assert complete.status_code == 303

    db_session.refresh(ticket)
    assert ticket.status == TicketStatusEnum.COMPLETE.value
    assert ticket.unit_price == Decimal("30.00")
    assert ticket.total == Decimal("300.00")


def test_customer_override_duplicate_active_pair_is_rejected(client, db_session):
    customer = Customer(account_code="C-OVR-DUP-1", name="Override Duplicate Customer")
    unit = Unit(name="Override Dup Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-OVR-DUP-1",
        description="Override Duplicate Product",
        unit=unit,
        unit_price=Decimal("5.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.flush()
    override = CustomerProductPrice(
        customer_id=customer.id,
        product_id=product.id,
        unit_price=Decimal("4.50"),
        is_active=True,
    )
    db_session.add(override)
    db_session.commit()

    response = client.post(
        f"/customers/{customer.id}/price-overrides",
        data={"product_id": str(product.id), "unit_price": "4.75"},
    )

    assert response.status_code == 400
    assert "Active override already exists for this customer and product." in response.text


def test_customer_override_basic_crud_flow(client, db_session):
    customer = Customer(account_code="C-OVR-CRUD-1", name="Override CRUD Customer")
    unit = Unit(name="Override CRUD Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-OVR-CRUD-1",
        description="Override CRUD Product",
        unit=unit,
        unit_price=Decimal("11.00"),
    )
    db_session.add_all([customer, unit, product])
    db_session.commit()

    create = client.post(
        f"/customers/{customer.id}/price-overrides",
        data={"product_id": str(product.id), "unit_price": "7.00"},
        follow_redirects=False,
    )
    assert create.status_code == 303
    assert create.headers["location"].endswith(f"/customers/{customer.id}?saved=1")

    override = db_session.execute(
        select(CustomerProductPrice).where(
            CustomerProductPrice.customer_id == customer.id,
            CustomerProductPrice.product_id == product.id,
        )
    ).scalar_one()
    assert override.is_active is True
    assert override.unit_price == Decimal("7.00")

    update = client.post(
        f"/customers/{customer.id}/price-overrides/{override.id}/update",
        data={
            "product_id": str(product.id),
            "unit_price": "8.25",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303
    assert update.headers["location"].endswith(f"/customers/{customer.id}?saved=1")

    db_session.refresh(override)
    assert override.is_active is True
    assert override.unit_price == Decimal("8.25")

    deactivate = client.post(
        f"/customers/{customer.id}/price-overrides/{override.id}/deactivate",
        follow_redirects=False,
    )
    assert deactivate.status_code == 303
    assert deactivate.headers["location"].endswith(f"/customers/{customer.id}?saved=1")

    db_session.refresh(override)
    assert override.is_active is False
