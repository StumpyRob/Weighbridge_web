import re
from datetime import date, datetime
from decimal import Decimal

from app.models import (
    Customer,
    CustomerProductPrice,
    DirectionEnum,
    Haulier,
    Invoice,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
    VehicleType,
)


def test_tickets_list_uses_net_header_and_compact_badges(client, db_session):
    customer = Customer(account_code="C-LIST-TICKET-1", name="Ticket List Customer")
    unit = Unit(name="List Ticket Unit", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="LIST-TICKET-VAT",
        description="List ticket VAT",
        rate_percent=Decimal("0.20"),
        is_active=True,
    )
    product = Product(
        code="P-LIST-TICKET-1",
        description="Ticket list product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("12.00"),
    )
    vehicle = Vehicle(registration="LT01ABC")
    db_session.add_all([customer, unit, tax_rate, product, vehicle])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-LIST-1",
        datetime=datetime(2026, 2, 18, 8, 30, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        product_id=product.id,
        walk_in_sale=True,
        net_kg=1250,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert '<th class="type-col">Type</th>' in response.text
    assert '<th class="weights-col numeric-col num">Net (kg)</th>' in response.text
    assert 'class="ticket-kind-badge ticket-kind-badge--sale"' in response.text
    assert "WALK-IN" in response.text
    assert "1,250 kg" in response.text
    assert 'class="btn btn--outline" href="/tickets">Reset</a>' in response.text


def test_tickets_list_shows_wtn_signature_status_badges(client, db_session):
    signed_waste = Ticket(
        ticket_no="T-LIST-WTN-SIGNED",
        datetime=datetime(2026, 2, 18, 8, 40, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        wtn_signature_data_uri="data:image/png;base64,signed",
        dont_invoice=False,
        paid=False,
    )
    unsigned_waste = Ticket(
        ticket_no="T-LIST-WTN-UNSIGNED",
        datetime=datetime(2026, 2, 18, 8, 39, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEOUT.value,
        dont_invoice=False,
        paid=False,
    )
    non_waste_sale = Ticket(
        ticket_no="T-LIST-WTN-SALE",
        datetime=datetime(2026, 2, 18, 8, 38, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.OUTWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add_all([signed_waste, unsigned_waste, non_waste_sale])
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert '<th class="wtn-col">WTN</th>' in response.text
    assert (
        re.search(
            rf'data-row-link="/tickets/{signed_waste.id}".*?'
            r'<td class="wtn-col">\s*<span class="status-pill status-complete">Signed</span>\s*</td>',
            response.text,
            re.S,
        )
        is not None
    )
    assert (
        re.search(
            rf'data-row-link="/tickets/{unsigned_waste.id}".*?'
            r'<td class="wtn-col">\s*<span class="status-pill status-draft">Unsigned</span>\s*</td>',
            response.text,
            re.S,
        )
        is not None
    )
    assert (
        re.search(
            rf'data-row-link="/tickets/{non_waste_sale.id}".*?'
            r'<td class="wtn-col">\s*&mdash;\s*</td>',
            response.text,
            re.S,
        )
        is not None
    )


def test_tickets_list_shows_ad_hoc_vehicle_registration_text(client, db_session):
    ticket = Ticket(
        ticket_no="T-LIST-ADHOC-1",
        datetime=datetime(2026, 2, 18, 9, 45, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        vehicle_reg_text="ABC123",
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get("/tickets")

    assert response.status_code == 200
    assert 'title="ABC123">ABC123</span>' in response.text


def test_tickets_list_pagination_links_render_as_buttons(client, db_session):
    for index in range(3):
        db_session.add(
            Ticket(
                ticket_no=f"T-LIST-PAGE-{index + 1}",
                datetime=datetime(2026, 2, 18, 8, 30 + index, 0),
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                dont_invoice=False,
                paid=False,
            )
        )
    db_session.commit()

    response = client.get("/tickets?page=2&page_size=1")

    assert response.status_code == 200
    assert 'class="btn btn--outline" href="/tickets?page=1">Previous</a>' in response.text
    assert 'class="btn btn--outline" href="/tickets?page=3">Next</a>' in response.text


def test_customers_list_hides_contact_columns_and_shows_operational_badges(
    client, db_session
):
    flagged_customer = Customer(
        account_code="C-LIST-CUST-1",
        name="List Customer",
        payment_terms_days=60,
        on_stop=True,
        do_not_invoice=True,
        must_have_po=True,
    )
    normal_customer = Customer(
        account_code="C-LIST-CUST-2",
        name="Normal Customer",
    )
    override_product = Product(
        code="P-LIST-CUST-OVR-1",
        description="List Customer Override Product",
        unit_price=Decimal("10.00"),
    )
    db_session.add_all([flagged_customer, normal_customer, override_product])
    db_session.flush()
    db_session.add_all(
        [
            CustomerProductPrice(
                customer_id=flagged_customer.id,
                product_id=override_product.id,
                unit_price=Decimal("9.50"),
                is_active=True,
            ),
            CustomerProductPrice(
                customer_id=normal_customer.id,
                product_id=override_product.id,
                unit_price=Decimal("8.75"),
                is_active=False,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/customers")

    assert response.status_code == 200
    assert "<th>Email</th>" not in response.text
    assert "<th>Phone</th>" not in response.text
    assert 'id="customer-terms-indicator-help"' in response.text
    assert 'id="customer-pricing-indicator-help"' in response.text
    assert "NET 60" in response.text
    assert "Has special pricing" in response.text
    assert "Yes (1)" in response.text
    assert response.text.count('class="pricing-indicator"') == 1
    assert "&#10003;" not in response.text
    assert "Flags" in response.text
    assert 'id="customer-flags-help"' in response.text
    assert "ON STOP" in response.text
    assert 'class="status-pill status-stop"' in response.text
    assert "NO INVOICE" in response.text
    assert "PO REQUIRED" in response.text
    assert "status-open\">Active" not in response.text
    assert "status-void\">Inactive" not in response.text
    assert "&mdash;" in response.text
    assert 'class="btn btn--outline" href="/customers">Reset</a>' in response.text


def test_vehicles_list_uses_vehicle_type_header_and_kg_formatting(client, db_session):
    vehicle_type = VehicleType(code="LIST-VTYPE", is_active=True)
    default_haulier = Haulier(name="List Default Haulier")
    db_session.add_all([vehicle_type, default_haulier])
    db_session.flush()
    vehicle = Vehicle(
        registration="LV01XYZ",
        vehicle_type_id=vehicle_type.id,
        default_haulier_id=default_haulier.id,
        default_tare_kg=12500,
        overweight_threshold_kg=32000,
    )
    db_session.add(vehicle)
    db_session.commit()

    response = client.get("/vehicles")

    assert response.status_code == 200
    assert "<th>Vehicle type</th>" in response.text
    assert '<th class="truncate-col">Default haulier</th>' in response.text
    assert "List Default Haulier" in response.text
    assert "12,500 kg" in response.text
    assert "32,000 kg" in response.text
    assert 'class="btn btn--outline" href="/vehicles">Reset</a>' in response.text


def test_products_units_list_shows_weight_system_label(client, db_session):
    weight_unit = Unit(name="List Weight Unit", unit_type="WEIGHT", is_active=True)
    db_session.add(weight_unit)
    db_session.commit()

    response = client.get("/products/units")

    assert response.status_code == 200
    assert "Weight (system)" in response.text
    assert "System</span>" not in response.text


def test_invoices_list_uses_status_badges_and_currency(client, db_session):
    customer = Customer(account_code="C-LIST-INV-1", name="Invoice List Customer")
    db_session.add(customer)
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-LIST-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 18),
        due_date=date(2026, 3, 18),
        status="DRAFT",
        net_total=Decimal("1234.50"),
        vat_total=Decimal("246.90"),
        gross_total=Decimal("1481.40"),
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get("/invoices")

    assert response.status_code == 200
    assert 'class="status-pill status-draft"' in response.text
    assert "&pound;1,234.50" in response.text
    assert "&pound;246.90" in response.text
    assert "&pound;1,481.40" in response.text
    assert '<form class="filters" method="get" action="/invoices">' in response.text
    assert 'class="btn btn--outline" href="/invoices">Reset</a>' in response.text
    assert "<h2>Filters</h2>" not in response.text
