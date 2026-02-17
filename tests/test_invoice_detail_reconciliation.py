import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Customer,
    DirectionEnum,
    EwcCode,
    Invoice,
    InvoiceLine,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)


def _make_customer(
    db_session,
    *,
    account_code: str,
    name: str,
    address_line1: str | None = None,
    address_line2: str | None = None,
    city: str | None = None,
    postcode: str | None = None,
    country: str | None = None,
) -> Customer:
    customer = Customer(
        account_code=account_code,
        name=name,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        postcode=postcode,
        country=country,
    )
    db_session.add(customer)
    db_session.flush()
    return customer


def _make_invoice(
    db_session,
    *,
    customer_id: int,
    invoice_no: str,
) -> Invoice:
    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer_id,
        invoice_date=date(2026, 2, 14),
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _make_product(
    db_session,
    *,
    code: str,
    description: str,
    unit_name: str,
    unit_type: str,
    unit_price: Decimal,
) -> Product:
    unit = Unit(name=unit_name, unit_type=unit_type, is_active=True)
    product = Product(
        code=code,
        description=description,
        unit=unit,
        unit_price=unit_price,
    )
    db_session.add_all([unit, product])
    db_session.flush()
    return product


def _make_ticket(
    db_session,
    *,
    ticket_no: str,
    dt: datetime,
    customer_id: int,
    product_id: int,
    invoice_id: int,
    po_number: str | None,
    vehicle_reg_text: str | None,
    qty,
    net_kg,
    unit_price: Decimal,
    total: Decimal,
    pricing_basis: str,
    pricing_unit_name: str,
    pricing_unit_type: str,
    transaction_type: str = TransactionTypeEnum.SALE.value,
    ewc_code_6: str | None = None,
    ewc_code_display: str | None = None,
    ewc_description: str | None = None,
    ewc_hazardous: bool | None = None,
    waste_producer_name: str | None = None,
    waste_producer_address: str | None = None,
    pricing_qty_snapshot=None,
    pricing_net_kg_snapshot=None,
    pricing_billable_qty_snapshot=None,
) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=dt,
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=transaction_type,
        customer_id=customer_id,
        product_id=product_id,
        invoice_id=invoice_id,
        po_number=po_number,
        vehicle_reg_text=vehicle_reg_text,
        ewc_code_6=ewc_code_6,
        ewc_code_display=ewc_code_display,
        ewc_description=ewc_description,
        ewc_hazardous=ewc_hazardous,
        waste_producer_name=waste_producer_name,
        waste_producer_address=waste_producer_address,
        qty=qty,
        net_kg=net_kg,
        unit_price=unit_price,
        total=total,
        pricing_basis=pricing_basis,
        pricing_unit_name=pricing_unit_name,
        pricing_unit_type=pricing_unit_type,
        pricing_qty_snapshot=pricing_qty_snapshot,
        pricing_net_kg_snapshot=pricing_net_kg_snapshot,
        pricing_billable_qty_snapshot=pricing_billable_qty_snapshot,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.flush()
    return ticket


def _add_invoice_line(
    db_session,
    *,
    invoice_id: int,
    ticket_id: int,
    description: str,
    quantity: Decimal,
    unit_price: Decimal,
    net: Decimal,
    vat: Decimal,
    gross: Decimal,
) -> None:
    line = InvoiceLine(
        invoice_id=invoice_id,
        ticket_id=ticket_id,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        net=net,
        vat=vat,
        gross=gross,
    )
    db_session.add(line)


def _linked_tickets_table_html(response_text: str) -> str:
    match = re.search(
        r'<table class="data-table invoice-linked-table">.*?</table>',
        response_text,
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def _linked_tickets_headers(response_text: str) -> list[str]:
    table_html = _linked_tickets_table_html(response_text)
    thead_match = re.search(r"<thead>.*?</thead>", table_html, re.DOTALL)
    assert thead_match is not None
    raw_headers = re.findall(r"<th[^>]*>\s*(.*?)\s*</th>", thead_match.group(0), re.DOTALL)
    headers: list[str] = []
    for value in raw_headers:
        text = re.sub(r"<[^>]+>", "", value).strip()
        headers.append(text)
    return headers


def test_invoice_detail_shows_single_po_account_and_billing_address(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-PO-1",
        name="Invoice Detail PO One",
        address_line1="1 Billing Road",
        city="Leeds",
        postcode="LS1 1AA",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-PO-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-PO-1",
        description="Bagged Aggregate",
        unit_name="Bags",
        unit_type="COUNT",
        unit_price=Decimal("12.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-PO-1",
        dt=datetime(2026, 2, 14, 10, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-SINGLE-1",
        vehicle_reg_text="AB12CDE",
        qty=Decimal("3.000"),
        net_kg=None,
        unit_price=Decimal("12.00"),
        total=Decimal("36.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Bags",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("3.000"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Ticket line",
        quantity=Decimal("3.000"),
        unit_price=Decimal("12.00"),
        net=Decimal("36.00"),
        vat=Decimal("0.00"),
        gross=Decimal("36.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "Account: C-INV-DET-PO-1" in response.text
    assert "1 Billing Road" in response.text
    assert "Leeds LS1 1AA" in response.text
    assert '<th>Date</th>' in response.text
    assert '<th>Product</th>' in response.text
    assert '<th>Net/Qty</th>' in response.text
    assert "<th>PO</th>" not in response.text
    assert "<th>Reg</th>" not in response.text
    assert "<th>Status</th>" not in response.text
    assert "<th>Date/time</th>" not in response.text
    assert f'href="/tickets/{ticket.id}"' in response.text
    assert "P-INV-DET-PO-1 - Bagged Aggregate" in response.text
    assert 'data-label="Net/Qty"' in response.text
    assert '<th>Description</th>' in response.text
    assert 'data-label="Qty"' in response.text
    assert 'data-label="Unit price"' in response.text
    assert "Subtotal:" in response.text
    assert "VAT:" in response.text
    assert "Total:" in response.text


def test_invoice_detail_shows_multiple_pos_in_linked_ticket_rows(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-PO-2",
        name="Invoice Detail PO Many",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-PO-2",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-PO-2",
        description="Mixed Load",
        unit_name="Loads",
        unit_type="COUNT",
        unit_price=Decimal("50.00"),
    )
    first = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-PO-2A",
        dt=datetime(2026, 2, 14, 11, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-A-100",
        vehicle_reg_text="XY11AAA",
        qty=Decimal("1.000"),
        net_kg=None,
        unit_price=Decimal("50.00"),
        total=Decimal("50.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("1.000"),
    )
    second = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-PO-2B",
        dt=datetime(2026, 2, 14, 12, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-B-200",
        vehicle_reg_text="XY22BBB",
        qty=Decimal("1.000"),
        net_kg=None,
        unit_price=Decimal("50.00"),
        total=Decimal("50.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("1.000"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=first.id,
        description="Ticket line A",
        quantity=Decimal("1.000"),
        unit_price=Decimal("50.00"),
        net=Decimal("50.00"),
        vat=Decimal("0.00"),
        gross=Decimal("50.00"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=second.id,
        description="Ticket line B",
        quantity=Decimal("1.000"),
        unit_price=Decimal("50.00"),
        net=Decimal("50.00"),
        vat=Decimal("0.00"),
        gross=Decimal("50.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>PO</th>" not in response.text
    assert "PO-A-100" not in response.text
    assert "PO-B-200" not in response.text
    assert '<th>Date</th>' in response.text
    assert '<th>Product</th>' in response.text
    assert '<th>Net/Qty</th>' in response.text


def test_invoice_detail_shows_dash_when_no_po_present(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-PO-3",
        name="Invoice Detail No PO",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-PO-3",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-PO-3",
        description="Loose Material",
        unit_name="Loads",
        unit_type="COUNT",
        unit_price=Decimal("80.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-PO-3",
        dt=datetime(2026, 2, 14, 13, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="ZZ11ZZZ",
        qty=Decimal("1.000"),
        net_kg=None,
        unit_price=Decimal("80.00"),
        total=Decimal("80.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("1.000"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Ticket line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("80.00"),
        net=Decimal("80.00"),
        vat=Decimal("0.00"),
        gross=Decimal("80.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>PO</th>" not in response.text
    assert "<th>Date/time</th>" not in response.text
    assert re.search(r'data-label="Date">\s*14/02/2026\s*</td>', response.text)
    assert "13:00" not in response.text


def test_invoice_detail_sales_only_hides_waste_compliance_columns(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-HIDE-1",
        name="Invoice Detail Sales Only",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-HIDE-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-HIDE-1",
        description="Sales Product",
        unit_name="Loads",
        unit_type="COUNT",
        unit_price=Decimal("20.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-HIDE-1",
        dt=datetime(2026, 2, 14, 14, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-SALE-ONLY-1",
        vehicle_reg_text="SA11EEE",
        qty=Decimal("1.000"),
        net_kg=None,
        unit_price=Decimal("20.00"),
        total=Decimal("20.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("1.000"),
        transaction_type=TransactionTypeEnum.SALE.value,
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Sales line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("20.00"),
        net=Decimal("20.00"),
        vat=Decimal("0.00"),
        gross=Decimal("20.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>PO</th>" not in response.text
    assert "<th>Reg</th>" not in response.text
    assert "<th>Status</th>" not in response.text
    assert "<th>Date/time</th>" not in response.text
    assert "<th>EWC</th>" not in response.text
    assert "<th>Hazard</th>" not in response.text
    assert "<th>Waste Producer</th>" not in response.text
    assert "<th>Compliance</th>" not in response.text
    assert "<th>Date</th>" in response.text
    assert "P-INV-DET-WASTE-HIDE-1 - Sales Product" in response.text
    assert f'href="/tickets/{ticket.id}"' in response.text
    assert 'data-label="Net/Qty"' in response.text


def test_invoice_detail_contract_non_waste_linked_ticket_columns_are_exact(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-CONTRACT-NONWASTE-1",
        name="Invoice Detail Contract Non Waste",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-CONTRACT-NONWASTE-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-CONTRACT-NONWASTE-1",
        description="Contract Product",
        unit_name="Loads",
        unit_type="COUNT",
        unit_price=Decimal("20.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-CONTRACT-NONWASTE-1",
        dt=datetime(2026, 2, 14, 14, 45, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="CN11WST",
        qty=Decimal("2.000"),
        net_kg=None,
        unit_price=Decimal("20.00"),
        total=Decimal("40.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("2.000"),
        transaction_type=TransactionTypeEnum.SALE.value,
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Contract non-waste line",
        quantity=Decimal("2.000"),
        unit_price=Decimal("20.00"),
        net=Decimal("40.00"),
        vat=Decimal("0.00"),
        gross=Decimal("40.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert _linked_tickets_headers(response.text) == [
        "Ticket",
        "Date",
        "Product",
        "Net/Qty",
        "Total",
    ]
    for forbidden in (
        "PO",
        "Reg",
        "Status",
        "Date/time",
        "Waste Producer",
        "Hazard",
    ):
        assert f"<th>{forbidden}</th>" not in response.text
    assert re.search(r'data-label="Date">\s*14/02/2026\s*</td>', response.text)
    assert "14/02/2026 14:45" not in response.text
    assert f'href="/tickets/{ticket.id}"' in response.text
    assert "P-INV-DET-CONTRACT-NONWASTE-1 - Contract Product" in response.text
    assert "2 x Loads" in response.text


def test_invoice_detail_contract_waste_linked_ticket_columns_and_compliance_are_exact(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-CONTRACT-WASTE-1",
        name="Invoice Detail Contract Waste",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-CONTRACT-WASTE-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-CONTRACT-WASTE-1",
        description="Contract Waste Product",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("12.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-CONTRACT-WASTE-1",
        dt=datetime(2026, 2, 14, 15, 20, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="CW11AST",
        qty=None,
        net_kg=Decimal("2500"),
        unit_price=Decimal("12.00"),
        total=Decimal("30.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("2.500"),
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="05 06 01",
        ewc_hazardous=False,
        waste_producer_name="Contract Waste Producer",
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Contract waste line",
        quantity=Decimal("2.500"),
        unit_price=Decimal("12.00"),
        net=Decimal("30.00"),
        vat=Decimal("0.00"),
        gross=Decimal("30.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert _linked_tickets_headers(response.text) == [
        "Ticket",
        "Date",
        "Product",
        "Compliance",
        "Net/Qty",
        "Total",
    ]
    for forbidden in (
        "PO",
        "Reg",
        "Status",
        "Date/time",
        "Waste Producer",
        "Hazard",
    ):
        assert f"<th>{forbidden}</th>" not in response.text
    assert re.search(r'data-label="Date">\s*14/02/2026\s*</td>', response.text)
    assert "14/02/2026 15:20" not in response.text
    assert f'href="/tickets/{ticket.id}"' in response.text
    assert "P-INV-DET-CONTRACT-WASTE-1 - Contract Waste Product" in response.text
    assert "2.500 tonnes" in response.text
    assert re.search(r"EWC:</span>\s*05 06 01", response.text)
    assert re.search(r"Hazard:</span>\s*No", response.text)
    assert re.search(r"Producer:</span>\s*Contract Waste Producer", response.text)


def test_invoice_detail_contract_mobile_data_labels_present(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-CONTRACT-LABELS-1",
        name="Invoice Detail Contract Labels",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-CONTRACT-LABELS-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-CONTRACT-LABELS-1",
        description="Contract Labels Product",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("9.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-CONTRACT-LABELS-1",
        dt=datetime(2026, 2, 14, 12, 30, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="CL11LBL",
        qty=None,
        net_kg=Decimal("4000"),
        unit_price=Decimal("9.00"),
        total=Decimal("36.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("4.000"),
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        ewc_code_display=None,
        waste_producer_name=None,
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Contract labels line",
        quantity=Decimal("4.000"),
        unit_price=Decimal("9.00"),
        net=Decimal("36.00"),
        vat=Decimal("0.00"),
        gross=Decimal("36.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    for label in ("Ticket", "Date", "Product", "Compliance", "Net/Qty", "Total"):
        assert f'data-label="{label}"' in response.text
    for label in ("Description", "Qty", "Unit price", "Net", "VAT", "Gross"):
        assert f'data-label="{label}"' in response.text


def test_invoice_detail_waste_invoice_shows_compliance_columns_and_values(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-SHOW-1",
        name="Invoice Detail Waste",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-SHOW-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-SHOW-1",
        description="Waste Product",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("15.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-SHOW-1",
        dt=datetime(2026, 2, 14, 16, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-WASTE-1",
        vehicle_reg_text="WA55TEE",
        qty=None,
        net_kg=Decimal("10000"),
        unit_price=Decimal("15.00"),
        total=Decimal("150.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("10.000"),
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="05 06 01*",
        ewc_description="Acid tars",
        waste_producer_name="Waste Producer Site A",
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Waste line",
        quantity=Decimal("10.000"),
        unit_price=Decimal("15.00"),
        net=Decimal("150.00"),
        vat=Decimal("0.00"),
        gross=Decimal("150.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Compliance</th>" in response.text
    assert "<th>EWC</th>" not in response.text
    assert "<th>Hazard</th>" not in response.text
    assert "<th>Waste Producer</th>" not in response.text
    assert "05 06 01*" in response.text
    assert "Waste Producer Site A" in response.text
    assert 'data-label="Compliance"' in response.text
    assert "Hazard:" in response.text
    assert re.search(r"Hazard:</span>\s*Yes", response.text)


def test_invoice_detail_mixed_invoice_shows_no_hazard_for_non_waste_rows(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-MIX-1",
        name="Invoice Detail Mixed Waste",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-MIX-1",
    )
    waste_product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-MIX-W",
        description="Waste Product",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("15.00"),
    )
    sale_product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-MIX-S",
        description="Sales Product",
        unit_name="Loads",
        unit_type="COUNT",
        unit_price=Decimal("20.00"),
    )
    waste_ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-MIX-W",
        dt=datetime(2026, 2, 14, 10, 0, 0),
        customer_id=customer.id,
        product_id=waste_product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="MX11WAS",
        qty=None,
        net_kg=Decimal("10000"),
        unit_price=Decimal("15.00"),
        total=Decimal("150.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("10.000"),
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="05 06 01",
        waste_producer_name="Waste Producer Mix",
    )
    sale_ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-MIX-S",
        dt=datetime(2026, 2, 14, 11, 0, 0),
        customer_id=customer.id,
        product_id=sale_product.id,
        invoice_id=invoice.id,
        po_number=None,
        vehicle_reg_text="MX22SAL",
        qty=Decimal("1.000"),
        net_kg=None,
        unit_price=Decimal("20.00"),
        total=Decimal("20.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Loads",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("1.000"),
        transaction_type=TransactionTypeEnum.SALE.value,
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=waste_ticket.id,
        description="Waste line",
        quantity=Decimal("10.000"),
        unit_price=Decimal("15.00"),
        net=Decimal("150.00"),
        vat=Decimal("0.00"),
        gross=Decimal("150.00"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=sale_ticket.id,
        description="Sale line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("20.00"),
        net=Decimal("20.00"),
        vat=Decimal("0.00"),
        gross=Decimal("20.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Compliance</th>" in response.text
    sale_row_match = re.search(
        rf'href="/tickets/{sale_ticket.id}">{sale_ticket.ticket_no}</a>.*?data-label="Compliance">(.*?)</td>',
        response.text,
        re.DOTALL,
    )
    assert sale_row_match
    sale_compliance_cell = sale_row_match.group(1)
    assert re.search(r"EWC:</span>\s*&mdash;", sale_compliance_cell)
    assert "Hazard:" in sale_compliance_cell
    assert re.search(r">\s*No\s*<", sale_compliance_cell)
    assert re.search(r"Producer:</span>\s*&mdash;", sale_compliance_cell)
    assert "05 06 01" not in sale_compliance_cell
    assert "Waste Producer Mix" not in sale_compliance_cell


def test_invoice_detail_waste_missing_ewc_renders_dash(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-MISS-1",
        name="Invoice Detail Waste Missing EWC",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-MISS-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-MISS-1",
        description="Waste Product Missing EWC",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("9.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-MISS-1",
        dt=datetime(2026, 2, 14, 17, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-WASTE-MISS-1",
        vehicle_reg_text="WM11ISS",
        qty=None,
        net_kg=Decimal("5000"),
        unit_price=Decimal("9.00"),
        total=Decimal("45.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("5.000"),
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        ewc_code_display=None,
        ewc_description=None,
        waste_producer_name="Waste Producer Site B",
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Waste line missing EWC",
        quantity=Decimal("5.000"),
        unit_price=Decimal("9.00"),
        net=Decimal("45.00"),
        vat=Decimal("0.00"),
        gross=Decimal("45.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Compliance</th>" in response.text
    assert "<th>EWC</th>" not in response.text
    assert "<th>Hazard</th>" not in response.text
    assert "<th>Waste Producer</th>" not in response.text
    assert re.search(r'EWC:</span>\s*&mdash;', response.text)
    assert re.search(r"Hazard:</span>\s*No", response.text)
    assert "Waste Producer Site B" in response.text


def test_invoice_detail_waste_hazard_detects_flag_without_star(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-HAZ-1",
        name="Invoice Detail Waste Hazard Flag",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-HAZ-1",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-HAZ-1",
        description="Hazard Flag Product",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("9.00"),
    )
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-HAZ-1",
        dt=datetime(2026, 2, 14, 18, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-WASTE-HAZ-1",
        vehicle_reg_text="WH44AZD",
        qty=None,
        net_kg=Decimal("7000"),
        unit_price=Decimal("9.00"),
        total=Decimal("63.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("7.000"),
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        ewc_code_6="123456",
        ewc_code_display=None,
        ewc_hazardous=True,
        waste_producer_name="Waste Producer Hazard",
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Waste line hazard flag",
        quantity=Decimal("7.000"),
        unit_price=Decimal("9.00"),
        net=Decimal("63.00"),
        vat=Decimal("0.00"),
        gross=Decimal("63.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Compliance</th>" in response.text
    assert "<th>EWC</th>" not in response.text
    assert "<th>Hazard</th>" not in response.text
    assert "12 34 56" in response.text
    assert "Hazard:" in response.text
    assert re.search(r"Hazard:</span>\s*Yes", response.text)


def test_invoice_detail_waste_hazard_detects_product_ewc_flag(client, db_session):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-WASTE-HAZ-2",
        name="Invoice Detail Waste Hazard Product",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-WASTE-HAZ-2",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-WASTE-HAZ-2",
        description="Hazard Product Flag",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("11.00"),
    )
    product.ewc_code = EwcCode(
        code_6="654321",
        code_display="65 43 21",
        description="Hazardous EWC",
        hazardous=True,
        active=True,
        source_file="tests",
        imported_at=datetime(2026, 2, 1, 0, 0, 0),
    )
    db_session.flush()
    ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-WASTE-HAZ-2",
        dt=datetime(2026, 2, 14, 19, 0, 0),
        customer_id=customer.id,
        product_id=product.id,
        invoice_id=invoice.id,
        po_number="PO-WASTE-HAZ-2",
        vehicle_reg_text="WH55AZE",
        qty=None,
        net_kg=Decimal("4000"),
        unit_price=Decimal("11.00"),
        total=Decimal("44.00"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_billable_qty_snapshot=Decimal("4.000"),
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_6="654321",
        ewc_code_display=None,
        waste_producer_name="Waste Producer Hazard Product",
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=ticket.id,
        description="Waste line hazard product flag",
        quantity=Decimal("4.000"),
        unit_price=Decimal("11.00"),
        net=Decimal("44.00"),
        vat=Decimal("0.00"),
        gross=Decimal("44.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Compliance</th>" in response.text
    assert "<th>EWC</th>" not in response.text
    assert "<th>Hazard</th>" not in response.text
    assert "65 43 21" in response.text
    assert "Hazard:" in response.text
    assert re.search(r"Hazard:</span>\s*Yes", response.text)


def test_invoice_detail_linked_tickets_show_reconciliation_columns_and_values(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-RECON-1",
        name="Invoice Detail Recon",
    )
    invoice = _make_invoice(
        db_session,
        customer_id=customer.id,
        invoice_no="INV-DET-RECON-1",
    )
    weight_product = _make_product(
        db_session,
        code="P-W-RECON-1",
        description="Rubble Waste",
        unit_name="tonnes",
        unit_type="WEIGHT",
        unit_price=Decimal("25.00"),
    )
    count_product = _make_product(
        db_session,
        code="P-C-RECON-1",
        description="Bulk Bags",
        unit_name="Bags",
        unit_type="COUNT",
        unit_price=Decimal("7.00"),
    )
    weight_ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-RECON-W",
        dt=datetime(2026, 2, 14, 8, 30, 0),
        customer_id=customer.id,
        product_id=weight_product.id,
        invoice_id=invoice.id,
        po_number="PO-W-1",
        vehicle_reg_text="WX11AAA",
        qty=None,
        net_kg=Decimal("49210"),
        unit_price=Decimal("25.00"),
        total=Decimal("1230.25"),
        pricing_basis="WEIGHT",
        pricing_unit_name="tonnes",
        pricing_unit_type="WEIGHT",
        pricing_net_kg_snapshot=Decimal("49210"),
        pricing_billable_qty_snapshot=Decimal("49.210"),
    )
    count_ticket = _make_ticket(
        db_session,
        ticket_no="T-INV-DET-RECON-C",
        dt=datetime(2026, 2, 14, 9, 0, 0),
        customer_id=customer.id,
        product_id=count_product.id,
        invoice_id=invoice.id,
        po_number="PO-C-1",
        vehicle_reg_text="WX22BBB",
        qty=Decimal("3.000"),
        net_kg=None,
        unit_price=Decimal("7.00"),
        total=Decimal("21.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Bags",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("3.000"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=weight_ticket.id,
        description="Weight line",
        quantity=Decimal("49.210"),
        unit_price=Decimal("25.00"),
        net=Decimal("1230.25"),
        vat=Decimal("0.00"),
        gross=Decimal("1230.25"),
    )
    _add_invoice_line(
        db_session,
        invoice_id=invoice.id,
        ticket_id=count_ticket.id,
        description="Count line",
        quantity=Decimal("3.000"),
        unit_price=Decimal("7.00"),
        net=Decimal("21.00"),
        vat=Decimal("0.00"),
        gross=Decimal("21.00"),
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "<th>Date</th>" in response.text
    assert "<th>Product</th>" in response.text
    assert "<th>Net/Qty</th>" in response.text
    assert "<th>Reg</th>" not in response.text
    assert "<th>PO</th>" not in response.text
    assert "<th>Status</th>" not in response.text
    assert "P-W-RECON-1 - Rubble Waste" in response.text
    assert "P-C-RECON-1 - Bulk Bags" in response.text
    assert "49.210 tonnes" in response.text
    assert "3 x Bags" in response.text
    assert 'data-label="Ticket"' in response.text
    assert 'data-label="Date"' in response.text
    assert 'data-label="Net/Qty"' in response.text
    assert "08:30" not in response.text
    assert "09:00" not in response.text


def test_invoice_line_description_includes_ticket_date_and_reg_on_confirm(
    client, db_session
):
    customer = _make_customer(
        db_session,
        account_code="C-INV-DET-LINE-1",
        name="Invoice Line Description",
    )
    product = _make_product(
        db_session,
        code="P-INV-DET-LINE-1",
        description="Rubble Waste",
        unit_name="Bags",
        unit_type="COUNT",
        unit_price=Decimal("10.00"),
    )
    ticket = Ticket(
        ticket_no="T-INV-DET-LINE-1",
        datetime=datetime(2026, 2, 14, 15, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        vehicle_reg_text="AB12CDE",
        qty=Decimal("2.000"),
        unit_price=Decimal("10.00"),
        total=Decimal("20.00"),
        pricing_basis="COUNT",
        pricing_unit_name="Bags",
        pricing_unit_type="COUNT",
        pricing_qty_snapshot=Decimal("2.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

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
    line = db_session.execute(
        select(InvoiceLine).where(InvoiceLine.ticket_id == ticket.id).limit(1)
    ).scalar_one()
    assert f"Ticket {ticket.ticket_no}" in line.description
    assert "14/02/2026" in line.description
    assert "AB12CDE" in line.description
