from datetime import date
from decimal import Decimal
from pathlib import Path
import re

import pytest
from sqlalchemy import delete
from urllib.parse import parse_qs, urlparse

from app.config import settings
from app.models import (
    Customer,
    Invoice,
    InvoiceLine,
    PrintJob,
    PrintProfile,
    PrintTemplate,
)
from app.services.pdf import (
    PdfRendererStatus,
    ensure_seed_invoice_pdf_template,
    render_invoice_pdf_html,
)
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
        account_code="C-PDF-TMPL-1",
        name="Invoice Template Customer",
        address_line1="1 Template Street",
        city="Leeds",
        postcode="LS1 1AA",
    )
    db_session.add(customer)
    db_session.flush()

    invoice = Invoice(
        invoice_no="INV-TMPL-1",
        customer_id=customer.id,
        invoice_date=date(2026, 2, 22),
        due_date=date(2026, 3, 1),
        status="DRAFT",
        net_total=Decimal("100.00"),
        vat_total=Decimal("20.00"),
        gross_total=Decimal("120.00"),
        customer_snapshot_json={"vat_number": "GB-TMPL-1"},
    )
    db_session.add(invoice)
    db_session.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        ticket_id=77,
        description="Template line",
        quantity=Decimal("1.000"),
        unit_price=Decimal("100.00"),
        net=Decimal("100.00"),
        vat=Decimal("20.00"),
        gross=Decimal("120.00"),
        product_snapshot_json={"final_disposal_wip": False, "used_on_site_wip": True},
    )
    db_session.add(line)
    db_session.commit()
    return invoice


def test_invoice_pdf_html_debug_route_is_dev_only(client, db_session, monkeypatch):
    invoice = _make_invoice_with_line(db_session)
    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "debug", False)

    templates.env.globals["DEV_MODE"] = False
    try:
        blocked = client.get(f"/invoices/{invoice.id}/pdf/html")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode
    assert blocked.status_code == 404

    templates.env.globals["DEV_MODE"] = True
    try:
        allowed = client.get(f"/invoices/{invoice.id}/pdf/html")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode
    assert allowed.status_code == 200
    assert "<html" in allowed.text.lower()
    assert invoice.invoice_no in allowed.text


def test_invoice_pdf_template_preview_uses_invoice_id_context(client, db_session):
    invoice = _make_invoice_with_line(db_session)
    template = PrintTemplate(
        code="INV_A4_PREVIEW",
        description="Invoice preview template",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>INVPREVIEW {{ invoice.invoice_no }} {{ totals.gross }}</body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get(
        f"/admin/printing/templates/{template.id}/preview",
        params={"invoice_id": invoice.id},
    )
    assert response.status_code == 200
    assert "Previewing with Invoice:" in response.text
    assert "INVPREVIEW" in response.text
    assert invoice.invoice_no in response.text


def test_admin_set_default_invoice_pdf_template_changes_invoice_html_render(
    client,
    db_session,
):
    invoice = _make_invoice_with_line(db_session)
    template = PrintTemplate(
        code="INVOICE_A4_DEFAULT",
        description="Default A4 invoice PDF template",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>PDF-MARKER {{ invoice.invoice_no }} {{ customer.vat_number }}</body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{template.id}/set-default-invoice-pdf",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    rendered_html = render_invoice_pdf_html(invoice.id, db_session)
    assert "PDF-MARKER" in rendered_html
    assert invoice.invoice_no in rendered_html
    assert "GB-TMPL-1" in rendered_html

    pdf_response = client.get(f"/invoices/{invoice.id}/pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"].startswith("application/pdf")


def test_invoice_detail_hides_manage_invoice_pdf_templates_action(
    client,
    db_session,
):
    invoice = _make_invoice_with_line(db_session)
    original_dev_mode = templates.env.globals.get("DEV_MODE", False)

    templates.env.globals["DEV_MODE"] = False
    try:
        non_dev = client.get(f"/invoices/{invoice.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode
    assert non_dev.status_code == 200
    assert "/admin/printing/templates?purpose=INVOICE_PDF" not in non_dev.text

    templates.env.globals["DEV_MODE"] = True
    try:
        dev = client.get(f"/invoices/{invoice.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode
    assert dev.status_code == 200
    assert "Advanced printing" in dev.text
    assert 'href="/admin/printing/templates?purpose=INVOICE_PDF"' not in dev.text
    assert "Download PDF" in dev.text
    assert "Send to Printer" in dev.text


def test_invoice_template_new_form_shows_invoice_tokens_without_global_validation_hint(client):
    response = client.get("/admin/printing/invoice-pdf-templates/new")
    assert response.status_code == 200
    assert "Insert Default Invoice PDF Layout" in response.text
    assert "{{ invoice.invoice_no }}" in response.text
    assert "{{ customer.vat_number }}" in response.text
    assert "{{ totals.gross }}" in response.text
    assert "sample_invoice_id" in response.text
    assert "Provide a sample invoice/ticket to validate render." not in response.text


def test_invoice_template_save_with_sample_invoice_runs_render_validation(
    client,
    db_session,
):
    invoice = _make_invoice_with_line(db_session)
    response = client.post(
        "/admin/printing/templates/new",
        data={
            "code": "INV_RENDER_FAIL_WITH_SAMPLE",
            "description": "Sample render validation",
            "purpose": "INVOICE_PDF",
            "content_type": "HTML",
            "content": "<html><body>{{ 1 / 0 }}</body></html>",
            "sample_invoice_id": str(invoice.id),
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Template render failed" in response.text


def test_ensure_seed_invoice_pdf_template_upgrades_legacy_default_without_logo(db_session):
    builtin_content = (
        Path("app/templates/invoices/pdf.html").read_text(encoding="utf-8").strip()
    )
    legacy_content = re.sub(
        r"\s*\{%\s*if company\.logo_url\s*%\}\s*"
        r"<img[^>]*class=\"company-logo\"[^>]*>\s*"
        r"\{%\s*endif\s*%\}\s*",
        "\n",
        builtin_content,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    template = PrintTemplate(
        code="INVOICE_A4_DEFAULT",
        description="Default A4 invoice PDF template",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content=legacy_content,
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    _, changed = ensure_seed_invoice_pdf_template(db_session)
    assert changed is True

    db_session.refresh(template)
    assert "company.logo_url" in template.content


def test_set_default_template_forces_template_active(client, db_session):
    template = PrintTemplate(
        code="INV_DEFAULT_FORCE_ACTIVE",
        description="Default force active",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>{{ invoice.invoice_no }}</body></html>",
        is_active=False,
    )
    db_session.add(template)
    db_session.commit()

    response = client.post(
        f"/admin/printing/templates/{template.id}/set-default",
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(template)
    assert template.is_active is True


def test_cannot_deactivate_default_template(client, db_session):
    template = PrintTemplate(
        code="TMPL_DEFAULT_PROTECT",
        description="Protected default template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{template.id}/set-default",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    blocked = client.post(
        f"/admin/printing/templates/{template.id}/deactivate",
        follow_redirects=True,
    )
    assert blocked.status_code == 200
    assert "You can&#39;t deactivate/delete the default template." in blocked.text
    db_session.refresh(template)
    assert template.is_active is True


def test_cannot_delete_default_template(client, db_session):
    template = PrintTemplate(
        code="TMPL_DEFAULT_DELETE_PROTECT",
        description="Protected default template delete",
        purpose="TICKET_A4",
        content_type="HTML",
        content="<html><body>{{ ticket.number }}</body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{template.id}/set-default",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    blocked = client.post(
        f"/admin/printing/templates/{template.id}/delete",
        follow_redirects=True,
    )
    assert blocked.status_code == 200
    assert "You can&#39;t deactivate/delete the default template." in blocked.text
    assert db_session.get(PrintTemplate, template.id) is not None


def test_default_template_row_hides_deactivate_and_delete_actions(client, db_session):
    template = PrintTemplate(
        code="TMPL_DEFAULT_ROW_GUARD",
        description="Default row guard template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{template.id}/set-default",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    list_page = client.get("/admin/printing/templates?purpose=TICKET_THERMAL")
    assert list_page.status_code == 200
    assert "Default template cannot be deactivated." in list_page.text
    assert "Default template cannot be deleted." in list_page.text


def test_invoice_print_dispatch_creates_job_and_redirects_to_browser_print(
    client,
    db_session,
):
    invoice = _make_invoice_with_line(db_session)
    template = PrintTemplate(
        code="INV_PRINT_ROUTE_DEFAULT",
        description="Invoice print default",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>PRINT {{ invoice.invoice_no }}</body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{template.id}/set-default",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    db_session.add(
        PrintProfile(
            code="INV_PRINT_ROUTE_PROFILE",
            description="Invoice route profile",
            purpose="INVOICE_PDF",
            template_id=template.id,
            template_name="invoice_default.html",
            transport_mode="LOCAL_BROWSER",
            transport_config={},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    response = client.post(
        f"/invoices/{invoice.id}/print",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/invoices/{invoice.id}/print/browser?")
    query = parse_qs(urlparse(location).query)
    assert query.get("invoice_print_sent") == ["1"]
    assert query.get("invoice_print_job_id")

    job_id = int(query["invoice_print_job_id"][0])
    job = db_session.get(PrintJob, job_id)
    assert job is not None
    assert job.status == "SENT"
    assert job.purpose == "INVOICE_PDF"


def test_invoice_print_browser_accepts_empty_profile_id_query(client, db_session):
    invoice = _make_invoice_with_line(db_session)

    response = client.get(
        f"/invoices/{invoice.id}/print/browser",
        params={"purpose": "INVOICE_PDF", "profile_id": ""},
    )

    assert response.status_code == 200
    assert "window.print()" in response.text


def test_invoice_preview_route_uses_default_invoice_pdf_template_not_profile_template(
    client,
    db_session,
):
    invoice = _make_invoice_with_line(db_session)
    default_template = PrintTemplate(
        code="INV_PREVIEW_DEFAULT_TEMPLATE",
        description="Default preview template",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>DEFAULT-PREVIEW-MARKER {{ invoice.invoice_no }}</body></html>",
        is_active=True,
    )
    profile_template = PrintTemplate(
        code="INV_PREVIEW_PROFILE_TEMPLATE",
        description="Profile preview template",
        purpose="INVOICE_PDF",
        content_type="HTML",
        content="<html><body>PROFILE-PREVIEW-MARKER {{ invoice.invoice_no }}</body></html>",
        is_active=True,
    )
    db_session.add_all([default_template, profile_template])
    db_session.flush()

    profile = PrintProfile(
        code="INV_PREVIEW_PROFILE_ONLY",
        description="Invoice preview profile template",
        purpose="INVOICE_PDF",
        template_id=profile_template.id,
        template_name="profile-preview-template.html",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    set_default = client.post(
        f"/admin/printing/templates/{default_template.id}/set-default-invoice-pdf",
        follow_redirects=False,
    )
    assert set_default.status_code == 303

    response = client.get(
        f"/invoices/{invoice.id}/preview",
        params={"profile_id": profile.id},
    )
    assert response.status_code == 200
    assert "DEFAULT-PREVIEW-MARKER" in response.text
    assert "PROFILE-PREVIEW-MARKER" not in response.text


def test_invoice_pdf_route_fails_loud_in_dev_when_no_default_template(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.invoices as invoices_routes

    invoice = _make_invoice_with_line(db_session)
    db_session.execute(
        delete(PrintProfile).where(PrintProfile.purpose == "INVOICE_PDF")
    )
    db_session.execute(
        delete(PrintTemplate).where(PrintTemplate.purpose == "INVOICE_PDF")
    )
    db_session.commit()

    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(
        invoices_routes,
        "check_invoice_pdf_renderer",
        lambda: PdfRendererStatus(available=True, detail=None),
    )
    response = client.get(f"/invoices/{invoice.id}/pdf")
    assert response.status_code == 500
    assert response.json()["detail"] == "No default invoice PDF template configured"


def test_invoice_pdf_route_returns_500_when_renderer_unavailable(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.invoices as invoices_routes

    invoice = _make_invoice_with_line(db_session)
    monkeypatch.setattr(
        invoices_routes,
        "check_invoice_pdf_renderer",
        lambda: PdfRendererStatus(available=False, detail="missing system libraries"),
    )
    response = client.get(f"/invoices/{invoice.id}/pdf")
    assert response.status_code == 500
    assert f"Renderer unavailable for invoice {invoice.id}" in response.json()["detail"]
