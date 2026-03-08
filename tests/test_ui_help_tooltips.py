from datetime import datetime
from decimal import Decimal

from app.models import (
    Customer,
    DirectionEnum,
    PrintTemplate,
    Product,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    Vehicle,
)
from app.seed import seed_vehicle_types


def test_base_loads_help_tooltip_layer_script(client):
    response = client.get("/tickets")

    assert response.status_code == 200
    assert '<script src="/static/js/help_tooltips.js?v=' in response.text


def test_product_edit_uses_tooltips_and_removes_old_nominal_helper(client, db_session):
    unit = Unit(name="Tooltip Unit", unit_type="COUNT", is_active=True)
    product = Product(
        code="P-HELP-1",
        description="Tooltip Product",
        unit=unit,
        unit_price=Decimal("9.99"),
    )
    db_session.add_all([unit, product])
    db_session.commit()

    response = client.get(f"/products/{product.id}")

    assert response.status_code == 200
    assert 'id="product-group-help"' in response.text
    assert 'id="product-nominal-code-help"' in response.text
    assert 'id="product-ewc-code-help"' in response.text
    assert 'id="product-default-destination-help"' in response.text
    assert 'id="product-sales-only-help"' in response.text
    assert 'id="product-hazardous-help"' in response.text
    assert "Leave blank to inherit the Product Group default nominal code." in response.text
    assert "If blank, will use Product Group default (if set)." not in response.text


def test_customer_edit_shows_tooltips_for_billing_and_flags(client, db_session):
    customer = Customer(account_code="C-HELP-1", name="Tooltip Customer")
    db_session.add(customer)
    db_session.commit()

    response = client.get(f"/customers/{customer.id}")

    assert response.status_code == 200
    assert 'id="customer-invoice-frequency-help"' in response.text
    assert 'id="customer-payment-terms-days-help"' in response.text
    assert 'id="customer-payment-terms-help"' in response.text
    assert 'id="customer-on-stop-help"' in response.text
    assert 'id="customer-do-not-invoice-help"' in response.text
    assert 'id="customer-must-have-po-help"' in response.text
    assert 'id="customer-price-overrides-help"' in response.text
    assert 'id="customer-override-product-help"' in response.text
    assert 'id="customer-override-price-help"' in response.text
    assert 'id="customer-credit-summary-help"' in response.text
    assert 'id="customer-credit-summary-limit-help"' in response.text
    assert 'id="customer-credit-summary-outstanding-help"' in response.text
    assert 'id="customer-credit-summary-available-help"' in response.text
    assert 'id="customer-credit-summary-status-help"' in response.text
    assert 'id="customer-account-adjustments-help"' in response.text
    assert 'id="customer-adjustment-amount-help"' in response.text
    assert 'id="customer-adjustment-reason-help"' in response.text
    assert 'id="customer-adjustment-note-help"' in response.text
    assert "Used to suggest invoice date ranges when generating invoices." in response.text
    assert "Days from invoice date used to calculate due date." in response.text


def test_ticket_edit_shows_tooltips_and_no_inline_dont_invoice_hint(client, db_session):
    ticket = Ticket(
        ticket_no="T-HELP-1",
        datetime=datetime(2026, 2, 17, 10, 0, 0),
        status=TicketStatusEnum.OPEN.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="ticket-direction-help"' in response.text
    assert 'id="ticket-transaction-type-help"' in response.text
    assert 'id="ticket-walk-in-sale-help"' in response.text
    assert 'id="ticket-po-number-help"' in response.text
    assert 'id="ticket-readout-kg-help"' in response.text
    assert 'id="ticket-billable-quantity-help"' in response.text
    assert 'id="ticket-unit-rate-help"' in response.text
    assert 'id="ticket-dont-invoice-help"' in response.text
    assert 'id="ticket-yard-help"' in response.text
    assert 'id="ticket-area-help"' in response.text
    assert 'id="ticket-waste-producer-help"' in response.text
    assert 'id="ticket-ewc-code-help"' in response.text
    assert 'id="ticket-ewc-hazard-help"' in response.text
    assert 'id="ticket-void-reason-help"' in response.text
    assert "Use for cash/card counter sales. No customer invoice generated." in response.text
    assert "Locked on for walk-in sale." not in response.text


def test_admin_printing_pages_show_tooltips_for_non_obvious_fields(client, db_session):
    template = PrintTemplate(
        code="HELP-TPL-1",
        description="Help Tooltip Template",
        document_type="TICKET",
        format="TEXT",
        content="{{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    destinations_list = client.get("/admin/printing/destinations")
    assert destinations_list.status_code == 200
    assert 'id="printing-destinations-filter-document-type-help"' in destinations_list.text
    assert 'id="printing-destinations-col-template-help"' in destinations_list.text
    assert 'id="printing-destinations-col-default-help"' in destinations_list.text

    destination_form = client.get("/admin/printing/destinations/new")
    assert destination_form.status_code == 200
    assert 'id="printing-destination-document-type-help"' in destination_form.text
    assert 'id="printing-destination-template-help"' in destination_form.text
    assert 'id="printing-destination-delivery-type-help"' in destination_form.text
    assert 'id="printing-destination-is-default-help"' in destination_form.text
    assert 'id="printing-destination-is-active-help"' in destination_form.text
    assert 'id="printing-destination-advanced-json-help"' in destination_form.text

    templates_list = client.get("/admin/printing/templates")
    assert templates_list.status_code == 200
    assert 'id="printing-templates-filter-document-type-help"' in templates_list.text
    assert 'id="printing-templates-col-document-type-help"' in templates_list.text

    jobs_list = client.get("/admin/printing/jobs")
    assert jobs_list.status_code == 200
    assert 'id="printing-jobs-filter-status-help"' in jobs_list.text
    assert 'id="printing-jobs-col-status-help"' in jobs_list.text

    company_settings = client.get("/admin/company")
    assert company_settings.status_code == 200
    assert 'id="company-logo-upload-help"' in company_settings.text
    assert 'id="company-nav-logo-size-help"' in company_settings.text
    assert 'id="company-show-nav-logo-help"' in company_settings.text
    assert 'id="company-show-nav-title-help"' in company_settings.text
    assert 'id="company-navbar-color-help"' in company_settings.text
    assert 'id="company-primary-color-help"' in company_settings.text


def test_dashboard_home_shows_tooltips_for_operational_overview(client):
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="dashboard-open_tickets-help"' in response.text
    assert 'id="dashboard-completed_today-help"' in response.text
    assert 'id="dashboard-total_weight_today-help"' in response.text
    assert 'id="dashboard-invoices_pending-help"' in response.text
    assert 'id="dashboard-overview-activity-help"' in response.text
    assert 'id="dashboard-ticket-activity-help"' in response.text
    assert 'id="dashboard-weight-throughput-help"' in response.text
    assert 'id="dashboard-todays-traffic-help"' in response.text
    assert 'id="dashboard-recent-tickets-help"' in response.text
    assert 'id="dashboard-open-ticket-queue-help"' in response.text
    assert 'id="dashboard-invoice-activity-help"' in response.text
    assert "Use the period filter to switch the activity window for charts and activity panels below." in response.text
    assert "Shows completed net weight over time for the selected period so managers can track throughput, not just ticket count." in response.text


def test_vehicle_pages_show_tooltips_for_defaults_and_tares(client, db_session):
    seed_vehicle_types(db_session)
    vehicle = Vehicle(registration="VH-HELP-1")
    db_session.add(vehicle)
    db_session.commit()

    list_response = client.get("/vehicles")
    assert list_response.status_code == 200
    assert 'id="vehicle-list-registration-search-help"' in list_response.text
    assert 'id="vehicle-list-default-tare-help"' not in list_response.text
    assert 'id="vehicle-list-overweight-threshold-help"' not in list_response.text

    new_response = client.get("/vehicles/new")
    assert new_response.status_code == 200
    assert 'id="vehicle-default-tare-help"' in new_response.text
    assert 'id="vehicle-overweight-threshold-help"' in new_response.text
    assert 'id="vehicle-ticket-defaults-help"' in new_response.text
    assert 'id="vehicle-default-customer-help"' in new_response.text
    assert 'id="vehicle-default-haulier-help"' in new_response.text
    assert 'id="vehicle-default-driver-help"' in new_response.text
    assert "Typical Fleet Associations" not in new_response.text

    edit_response = client.get(f"/vehicles/{vehicle.id}")
    assert edit_response.status_code == 200
    assert 'id="vehicle-per-container-tares-help"' in edit_response.text
