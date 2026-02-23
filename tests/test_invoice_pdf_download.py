from datetime import date, datetime
from decimal import Decimal
import re

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import (
    Customer,
    DirectionEnum,
    Invoice,
    InvoiceLine,
    PrintProfile,
    Product,
    TaxRate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
)
from app.services.pdf import PdfRendererStatus, render_invoice_pdf_html
from app.templating import templates


@pytest.fixture(autouse=True)
def stub_invoice_pdf_renderer(monkeypatch):
    import app.routes.invoices as invoices_routes

    def _fake_render_invoice_pdf(invoice_id, db, **kwargs):
        html = render_invoice_pdf_html(
            invoice_id,
            db,
            template=kwargs.get("template"),
            allow_builtin_template_fallback=False,
        )
        assert "<html" in html.lower()
        return b"%PDF-1.4\n%stub-invoice-pdf\n"

    monkeypatch.setattr(
        invoices_routes,
        "check_invoice_pdf_renderer",
        lambda: PdfRendererStatus(available=True, detail=None),
    )
    monkeypatch.setattr(invoices_routes, "render_invoice_pdf", _fake_render_invoice_pdf)


def _make_invoice_with_line(db_session) -> Invoice:
    customer = Customer(
        account_code="C-PDF-1",
        name="PDF Customer",
        address_line1="1 Test Street",
        city="Leeds",
        postcode="LS1 1AA",
    )
    db_session.add(customer)
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-PDF-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 22),
        due_date=date(2026, 3, 8),
        status="DRAFT",
        net_total=Decimal("50.00"),
        vat_total=Decimal("10.00"),
        gross_total=Decimal("60.00"),
        customer_snapshot_json={
            "vat_number": "GB-PDF-OLD",
            "is_cash_account": True,
            "credit_limit_pence": 12500,
        },
    )
    db_session.add(invoice)
    db_session.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        ticket_id=101,
        description="PDF line item with snapshot flags",
        quantity=Decimal("2.000"),
        unit_price=Decimal("25.00"),
        net=Decimal("50.00"),
        vat=Decimal("10.00"),
        gross=Decimal("60.00"),
        product_snapshot_json={
            "final_disposal_wip": True,
            "used_on_site_wip": False,
        },
    )
    db_session.add(line)
    db_session.commit()
    return invoice


def test_invoice_pdf_download_route_returns_attachment_pdf_headers(client, db_session):
    invoice = _make_invoice_with_line(db_session)

    response = client.get(f"/invoices/{invoice.id}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert (
        response.headers.get("content-disposition", "")
        == 'attachment; filename="Invoice-INV-PDF-1.pdf"'
    )
    assert response.headers.get("cache-control", "") == "no-store, no-cache, must-revalidate, max-age=0"
    assert response.content.startswith(b"%PDF")


def test_invoice_pdf_download_returns_404_for_missing_invoice(client):
    response = client.get("/invoices/999999/pdf")

    assert response.status_code == 404
    assert response.json()["detail"] == "Invoice not found."


def test_invoice_pdf_download_returns_400_when_invoice_has_no_lines(client, db_session):
    customer = Customer(account_code="C-PDF-NOLINES", name="PDF No Lines Customer")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_no="INV-PDF-NOLINES",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 22),
        due_date=None,
        status="DRAFT",
        net_total=Decimal("0.00"),
        vat_total=Decimal("0.00"),
        gross_total=Decimal("0.00"),
        customer_snapshot_json={},
    )
    db_session.add(invoice)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}/pdf")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invoice has no line items."


def test_invoice_detail_page_shows_operator_print_actions(client, db_session):
    invoice = _make_invoice_with_line(db_session)

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = False
    try:
        response = client.get(f"/invoices/{invoice.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "Send to Printer" in response.text
    assert "Preview" in response.text
    assert "Preview (Browser Print)" not in response.text
    assert "Download PDF" in response.text
    assert f'action="/invoices/{invoice.id}/print"' in response.text
    assert f'formaction="/invoices/{invoice.id}/preview"' in response.text
    assert f'href="/invoices/{invoice.id}/pdf"' in response.text
    assert f'formaction="/invoices/{invoice.id}/print/browser"' not in response.text
    assert "Advanced printing" not in response.text
    assert 'id="print-profile-invoice-' not in response.text


def test_invoice_detail_auto_assigns_default_when_invoice_printer_exists(client, db_session):
    invoice = _make_invoice_with_line(db_session)
    existing_profiles = db_session.execute(
        select(PrintProfile).where(PrintProfile.purpose == "INVOICE_PDF")
    ).scalars().all()
    for profile in existing_profiles:
        profile.is_default = False
    db_session.add(
        PrintProfile(
            code="INV_NO_DEFAULT_PROFILE",
            description="Invoice no-default profile",
            purpose="INVOICE_PDF",
            template_name="invoice_default.html",
            transport_mode="LOCAL_BROWSER",
            transport_config={},
            is_default=False,
            is_active=True,
        )
    )
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "No printer configured. Contact admin." not in response.text
    send_button = re.search(
        r'(<button[^>]*data-print-actions-send[^>]*>\s*Send to Printer\s*</button>)',
        response.text,
    )
    assert send_button is not None
    assert "disabled" not in send_button.group(1).lower()
    refreshed = db_session.execute(
        select(PrintProfile).where(PrintProfile.code == "INV_NO_DEFAULT_PROFILE")
    ).scalar_one()
    assert refreshed.is_default is True


def test_invoice_detail_migrates_legacy_invoice_a4_profile_purpose(client, db_session):
    invoice = _make_invoice_with_line(db_session)
    legacy_profile = PrintProfile(
        code="INV_LEGACY_A4_PROFILE",
        description="Legacy invoice A4 profile",
        purpose="INVOICE_A4",
        template_name="invoice_default.html",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(legacy_profile)
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    db_session.refresh(legacy_profile)
    assert legacy_profile.purpose == "INVOICE_PDF"
    assert "Send to Printer" in response.text


def test_invoice_detail_shows_advanced_printing_menu_in_dev_mode(client, db_session):
    invoice = _make_invoice_with_line(db_session)
    db_session.add(
        PrintProfile(
            code="INV_DEV_ADVANCED",
            description="Invoice dev advanced profile",
            purpose="INVOICE_PDF",
            template_name="invoice_default.html",
            transport_mode="LOCAL_BROWSER",
            transport_config={},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = True
    try:
        response = client.get(f"/invoices/{invoice.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "Advanced printing" in response.text
    assert 'id="print-profile-invoice-' in response.text
    assert "Manage printers" not in response.text
    assert "View jobs" not in response.text
    assert "Templates" not in response.text
    assert "Test print" not in response.text


def test_invoice_preview_route_renders_browser_print_page(client, db_session):
    invoice = _make_invoice_with_line(db_session)

    response = client.get(f"/invoices/{invoice.id}/preview")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert f"Invoice Preview {invoice.invoice_no}" in response.text
    assert "window.print()" in response.text
    assert "no-print" in response.text
    assert f'href="/invoices/{invoice.id}"' in response.text


@pytest.mark.parametrize("status", ["DRAFT", "PAID", "VOID"])
def test_invoice_detail_keeps_print_actions_for_all_statuses(
    client,
    db_session,
    status: str,
):
    invoice = _make_invoice_with_line(db_session)
    invoice.status = status
    db_session.commit()

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "Download PDF" in response.text
    assert "Send to Printer" in response.text
    assert "Preview" in response.text
    assert "Preview (Browser Print)" not in response.text
    assert f'formaction="/invoices/{invoice.id}/preview"' in response.text
    assert f'href="/invoices/{invoice.id}/pdf"' in response.text
    if status == "VOID":
        assert "This invoice is VOID." in response.text


def test_invoice_pdf_html_uses_snapshots_not_live_customer_or_product(client, db_session):
    customer = Customer(
        account_code="C-PDF-SNAP-1",
        name="Snapshot PDF Customer",
        vat_number="GB-SNAPSHOT-OLD",
        credit_limit_pence=5000,
        is_cash_account=False,
    )
    unit = Unit(name="PDF Count Unit", unit_type="COUNT", is_active=True)
    tax_rate = TaxRate(
        code="PDF VAT 0",
        description="PDF VAT",
        rate_percent=Decimal("0.00"),
        is_active=True,
    )
    product = Product(
        code="P-PDF-SNAP-1",
        description="PDF Snapshot Product",
        unit=unit,
        tax_rate=tax_rate,
        unit_price=Decimal("15.00"),
        final_disposal_wip=True,
        used_on_site_wip=False,
    )
    db_session.add_all([customer, unit, tax_rate, product])
    db_session.flush()

    ticket = Ticket(
        ticket_no="T-PDF-SNAP-1",
        datetime=datetime(2026, 2, 22, 9, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        customer_id=customer.id,
        product_id=product.id,
        qty=2,
        unit_price=Decimal("15.00"),
        total=Decimal("30.00"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()

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

    invoice = db_session.execute(
        select(Invoice).order_by(Invoice.id.desc()).limit(1)
    ).scalar_one()

    html_before = render_invoice_pdf_html(invoice.id, db_session)
    assert "GB-SNAPSHOT-OLD" in html_before
    assert "Final disposal: Yes | Used on site: No" in html_before

    customer.vat_number = "GB-SNAPSHOT-NEW"
    product.final_disposal_wip = False
    product.used_on_site_wip = True
    db_session.commit()

    html_after = render_invoice_pdf_html(invoice.id, db_session)
    assert "GB-SNAPSHOT-OLD" in html_after
    assert "GB-SNAPSHOT-NEW" not in html_after
    assert "Final disposal: Yes | Used on site: No" in html_after
    assert "Final disposal: No | Used on site: Yes" not in html_after

    pdf_response = client.get(f"/invoices/{invoice.id}/pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")


def test_invoice_pdf_future_flags_default_off():
    settings = Settings(
        database_url="sqlite:///./test-pdf-settings.db",
        secret_key="test-secret",
    )

    assert settings.enable_invoice_pdf_emailing is False
    assert settings.enable_invoice_pdf_printing is False
