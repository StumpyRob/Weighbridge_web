from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

import app.services.printing as printing_service
from app.models import (
    AuditEvent,
    CompanySetting,
    Customer,
    Invoice,
    InvoiceLine,
    PlatformSetting,
    PrintDestination,
    PrintJob,
    PrintTemplate,
)
from app.services.email_service import EmailSendResult
from app.services.pdf import PdfRendererStatus


@pytest.fixture(autouse=True)
def stub_invoice_pdf_renderer(monkeypatch):
    import app.routes.invoices as invoices_routes

    monkeypatch.setattr(
        invoices_routes,
        "check_invoice_pdf_renderer",
        lambda: PdfRendererStatus(available=True, detail=None),
    )
    monkeypatch.setattr(
        invoices_routes,
        "render_invoice_pdf",
        lambda _invoice_id, _db, **_kwargs: b"%PDF-1.4\n%stub-invoice-pdf\n",
    )


def _create_invoice_with_line(
    db_session,
    *,
    invoice_no: str = "INV-PRINT-1",
    invoice_email: str | None = None,
    status: str = "DRAFT",
) -> Invoice:
    customer = Customer(
        account_code=f"AC-{invoice_no}",
        name=f"Customer {invoice_no}",
        invoice_email=invoice_email,
        address_line1="1 Test Street",
        city="Leeds",
        postcode="LS1 1AA",
    )
    db_session.add(customer)
    db_session.flush()

    invoice = Invoice(
        invoice_no=invoice_no,
        customer_id=customer.id,
        invoice_date=date(2026, 2, 22),
        due_date=date(2026, 3, 8),
        status=status,
        net_total=Decimal("50.00"),
        vat_total=Decimal("10.00"),
        gross_total=Decimal("60.00"),
        customer_snapshot_json={"vat_number": "GB-TEST"},
    )
    db_session.add(invoice)
    db_session.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        ticket_id=101,
        description="Invoice line",
        quantity=Decimal("2.000"),
        unit_price=Decimal("25.00"),
        net=Decimal("50.00"),
        vat=Decimal("10.00"),
        gross=Decimal("60.00"),
        product_snapshot_json={"used_on_site_wip": False},
    )
    db_session.add(line)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _create_template(
    db_session,
    *,
    code: str,
    content: str,
    document_type: str = "INVOICE",
    template_format: str = "HTML",
) -> PrintTemplate:
    template = PrintTemplate(
        code=code,
        description=code,
        document_type=document_type,
        format=template_format,
        content=content,
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)
    return template


def _create_destination(
    db_session,
    *,
    name: str,
    template_id: int,
    delivery_type: str,
    delivery_config: dict,
    is_default: bool = True,
) -> PrintDestination:
    destination = PrintDestination(
        name=name,
        description=name,
        document_type="INVOICE",
        template_id=template_id,
        delivery_type=delivery_type,
        delivery_config=delivery_config,
        is_default=is_default,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(destination)
    return destination


def _configure_platform_email(db_session) -> None:
    db_session.add(
        PlatformSetting(
            email_provider="resend",
            resend_api_key="re_test_api_key",
            from_email="platform@example.com",
            from_display_name="Weighbridge Web",
        )
    )
    db_session.commit()


def _upsert_company_setting(db_session, **values) -> CompanySetting:
    setting = db_session.execute(
        select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
    ).scalars().first()
    if setting is None:
        setting = CompanySetting()
        db_session.add(setting)
    for key, value in values.items():
        setattr(setting, key, value)
    db_session.commit()
    db_session.refresh(setting)
    return setting


def _invoice_email_audit_event(db_session, *, action: str, invoice_id: int) -> AuditEvent | None:
    return db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.action == action,
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == str(invoice_id),
        )
        .order_by(AuditEvent.id.desc())
    ).scalars().first()


def test_invoice_detail_documents_frame_groups_invoice_actions(client, db_session):
    _upsert_company_setting(
        db_session,
        name="Invoice Email Co",
        invoice_email_subject_template="",
        invoice_email_body_template="",
    )
    invoice = _create_invoice_with_line(
        db_session,
        invoice_no="INV-UI-1",
        invoice_email="billing@example.com",
    )
    template = _create_template(
        db_session,
        code="INV_UI_TEMPLATE",
        content="<html><body>{{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice Browser Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "Documents" in response.text
    assert "documents-panel" in response.text
    assert "documents-panel--header" in response.text
    assert "page-header__aside--documents" in response.text
    assert "invoice-meta-strip" in response.text
    assert "ticket-header-actions" not in response.text
    assert "Preview" in response.text
    assert "Download PDF" in response.text
    assert "Print" in response.text
    assert "Email Invoice" in response.text
    assert "Edit Email" in response.text
    assert 'data-document-email-primary-form' in response.text
    assert 'data-document-email-primary' in response.text
    assert f'action="/invoices/{invoice.id}/email"' in response.text
    assert 'name="to_email"' in response.text
    assert 'value="billing@example.com"' in response.text
    assert 'value="Invoice INV-UI-1 from Invoice Email Co"' in response.text
    assert "Hello," in response.text
    assert "Please find attached invoice INV-UI-1." in response.text
    assert "Regards," in response.text
    assert "Invoice Email Co" in response.text
    assert response.text.index('name="to_email"') < response.text.index('name="cc_email"')
    assert response.text.index('name="cc_email"') < response.text.index('name="subject"')
    assert response.text.index('name="subject"') < response.text.index('name="message"')
    assert "Status:" in response.text
    assert "Date:" in response.text
    assert "Due:" in response.text
    assert "Total:" in response.text
    assert "&pound;60.00" in response.text
    assert (
        "Browser printing may add URL/date/time headers/footers depending on your browser settings."
        not in response.text
    )
    assert response.text.index("Invoice INV-UI-1") < response.text.index("invoice-meta-strip")
    assert response.text.index("Documents") < response.text.index("Summary")
    assert "invoice-summary-contact" in response.text
    assert response.text.index("invoice-summary-contact") < response.text.index("invoice-summary-grid")
    assert response.text.index("Summary") < response.text.index("Customer INV-UI-1")
    assert response.text.index("Customer INV-UI-1") < response.text.index("1 Test Street")
    assert response.text.index("Summary") < response.text.index("1 Test Street")
    assert response.text.index("Void Invoice") < response.text.index('class="invoice-back-link"')
    assert "Advanced printing" not in response.text
    assert "Preview Invoice" not in response.text
    assert "Print locally (browser)" not in response.text


def test_invoice_detail_qz_print_button_uses_platform_ready_direct_print(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.invoices as invoices_routes

    monkeypatch.setattr(invoices_routes, "platform_qz_ready_for_tenants", lambda _db: True)

    invoice = _create_invoice_with_line(db_session, invoice_no="INV-QZ-1")
    template = _create_template(
        db_session,
        code="INV_QZ_TEMPLATE",
        content="<html><body>{{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice QZ Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={
            "qz_direct_print_enabled": True,
            "qz_printer_name": "Accounts Office Printer",
        },
    )

    response = client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert "data-qz-print-button" in response.text
    assert f'data-qz-pdf-url="/invoices/{invoice.id}/pdf"' in response.text
    assert 'data-qz-certificate-url="/qz/certificate"' in response.text
    assert 'data-qz-sign-url="/qz/sign"' in response.text
    assert 'data-qz-resolve-url="/printing/qz/resolve"' in response.text
    assert 'data-qz-workstation-register-url="/printing/qz/workstation/register"' in response.text
    assert 'data-qz-workstation-label-url="/printing/qz/workstation/label"' in response.text
    assert 'data-qz-document-type="INVOICE"' in response.text
    assert 'data-qz-printer-name="Accounts Office Printer"' in response.text
    assert f'data-qz-success-base-url="/invoices/{invoice.id}"' in response.text
    assert 'data-qz-success-kind="invoice"' in response.text
    assert "data-qz-workstation-note" in response.text


def test_invoice_primary_email_action_opens_form_when_customer_email_missing(client, db_session):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-UI-NOEMAIL-1", invoice_email=None)
    template = _create_template(
        db_session,
        code="INV_UI_NOEMAIL_TEMPLATE",
        content="<html><body>{{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice No Email Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )

    response = client.get(f"/invoices/{invoice.id}")
    assert response.status_code == 200
    assert f'href="/invoices/{invoice.id}?edit_email=1"' in response.text
    assert 'data-document-email-primary-form' not in response.text

    opened = client.get(f"/invoices/{invoice.id}?edit_email=1")
    assert opened.status_code == 200
    assert 'document-email-card" open' in opened.text


def test_invoice_send_by_email_uses_tenant_configured_subject_and_body_template(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.invoices as invoices_routes

    _upsert_company_setting(
        db_session,
        name="Template Company",
        invoice_email_subject_template="Configured {invoice_no} / {company_name}",
        invoice_email_body_template="Body for {invoice_no} from {company_name}",
    )
    invoice = _create_invoice_with_line(
        db_session,
        invoice_no="INV-TEMPLATE-1",
        invoice_email="billing@example.com",
    )
    template = _create_template(
        db_session,
        code="INV_TEMPLATE_EMAIL",
        content="<html><body>{{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice Template Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )
    _configure_platform_email(db_session)

    called: dict[str, object] = {}

    def _fake_send_email(**kwargs):
        called.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(invoices_routes, "send_email", _fake_send_email)

    detail_response = client.get(f"/invoices/{invoice.id}")
    assert detail_response.status_code == 200
    assert 'value="Configured INV-TEMPLATE-1 / Template Company"' in detail_response.text
    assert "Body for INV-TEMPLATE-1 from Template Company" in detail_response.text

    response = client.post(
        f"/invoices/{invoice.id}/email",
        data={"to_email": "customer@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert called["subject"] == "Configured INV-TEMPLATE-1 / Template Company"
    assert called["text_body"] == "Body for INV-TEMPLATE-1 from Template Company"


def test_invoice_preview_uses_default_destination_template(client, db_session):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-PREVIEW-1")
    template = _create_template(
        db_session,
        code="INV_PREVIEW_TEMPLATE",
        content="<html><body>INVOICE_PREVIEW_MARKER {{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice Preview Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )

    response = client.get(f"/invoices/{invoice.id}/preview")

    assert response.status_code == 200
    assert "INVOICE_PREVIEW_MARKER INV-PREVIEW-1" in response.text
    assert "class=\"no-print\"" in response.text


def test_invoice_print_email_destination_creates_job_and_calls_sender(
    client,
    db_session,
    monkeypatch,
):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-EMAIL-1")
    template = _create_template(
        db_session,
        code="INV_EMAIL_TEMPLATE",
        content="<html><body>EMAIL {{ invoice.invoice_no }}</body></html>",
    )
    destination = _create_destination(
        db_session,
        name="Invoice Email Destination",
        template_id=template.id,
        delivery_type="EMAIL_PDF",
        delivery_config={
            "to": "accounts@example.com",
            "email_subject_template": "Invoice {invoice_no}",
            "email_body_template": "Attached invoice {invoice_no}.",
            "attach_pdf": True,
        },
    )

    called: dict[str, object] = {}

    def _fake_send_delivery_email(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(printing_service, "send_delivery_email", _fake_send_delivery_email)

    response = client.post(f"/invoices/{invoice.id}/print", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/invoices/{invoice.id}?")

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("invoice_print_sent", [""])[0] == "1"

    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.invoice_id == invoice.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()

    assert job is not None
    assert job.status == "SENT"
    assert job.document_type == "INVOICE"
    assert job.destination_id == destination.id
    assert job.delivery_type == "EMAIL_PDF"

    assert called.get("document_type") == "INVOICE"
    assert called.get("invoice_id") == invoice.id
    assert isinstance(called.get("payload_bytes"), bytes)
    assert bytes(called["payload_bytes"]).startswith(b"%PDF")


def test_invoice_print_missing_default_destination_returns_clear_error_redirect(client, db_session):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-NODEST-1")

    response = client.post(f"/invoices/{invoice.id}/print", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/invoices/{invoice.id}?")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("print_failed", [""])[0] == "1"
    assert "Printing is not configured. Contact admin." in query.get("print_error_detail", [""])[0]


def test_invoice_pdf_download_route_returns_attachment_pdf_headers(client, db_session):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-PDF-1")
    template = _create_template(
        db_session,
        code="INV_PDF_TEMPLATE",
        content="<html><body>PDF {{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice PDF Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )

    response = client.get(f"/invoices/{invoice.id}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers.get("content-disposition", "") == 'attachment; filename="Invoice-INV-PDF-1.pdf"'
    assert response.content.startswith(b"%PDF")


def test_invoice_send_by_email_happy_path_attaches_pdf_and_writes_audit(
    client,
    db_session,
    monkeypatch,
):
    import app.routes.invoices as invoices_routes

    invoice = _create_invoice_with_line(
        db_session,
        invoice_no="INV-SEND-1",
        invoice_email="billing@example.com",
    )
    template = _create_template(
        db_session,
        code="INV_SEND_TEMPLATE",
        content="<html><body>SEND {{ invoice.invoice_no }}</body></html>",
    )
    _create_destination(
        db_session,
        name="Invoice Send Destination",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
    )
    _configure_platform_email(db_session)

    called: dict[str, object] = {}

    def _fake_send_email(**kwargs):
        called.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(
        invoices_routes,
        "send_email",
        _fake_send_email,
    )

    response = client.post(
        f"/invoices/{invoice.id}/email",
        data={
            "to_email": "customer@example.com",
            "cc_email": "accounts@example.com; finance@example.com",
            "subject": "Invoice INV-SEND-1",
            "message": "Attached invoice INV-SEND-1.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/invoices/{invoice.id}?email_sent=1"
    assert called["to"] == "customer@example.com"
    assert called["cc"] == ["accounts@example.com", "finance@example.com"]
    assert called["subject"] == "Invoice INV-SEND-1"
    assert called["text_body"] == "Attached invoice INV-SEND-1."

    attachments = called["attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.filename == "Invoice-INV-SEND-1.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.content_bytes.startswith(b"%PDF")

    audit_event = _invoice_email_audit_event(
        db_session,
        action="INVOICE_EMAIL_SENT",
        invoice_id=invoice.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("invoice_no") == "INV-SEND-1"
    assert audit_event.details_json.get("recipient") == "customer@example.com"
    assert audit_event.details_json.get("cc") == [
        "accounts@example.com",
        "finance@example.com",
    ]
    assert audit_event.details_json.get("subject") == "Invoice INV-SEND-1"
    assert audit_event.details_json.get("status") == "sent"


def test_invoice_send_by_email_rejects_invalid_email_and_keeps_page_rendered(
    client,
    db_session,
):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-SEND-ERR-1")

    response = client.post(
        f"/invoices/{invoice.id}/email",
        data={"to_email": "not-an-email"},
    )

    assert response.status_code == 400
    assert "To email must be a valid email address." in response.text
    assert "Email Invoice" in response.text

    audit_event = _invoice_email_audit_event(
        db_session,
        action="INVOICE_EMAIL_FAILED",
        invoice_id=invoice.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("error") == "To email must be a valid email address."


def test_invoice_send_by_email_blocks_void_invoice(client, db_session):
    invoice = _create_invoice_with_line(
        db_session,
        invoice_no="INV-VOID-EMAIL-1",
        status="VOID",
    )

    response = client.post(
        f"/invoices/{invoice.id}/email",
        data={"to_email": "customer@example.com"},
    )

    assert response.status_code == 400
    assert "Void invoices cannot be sent by email." in response.text

    audit_event = _invoice_email_audit_event(
        db_session,
        action="INVOICE_EMAIL_FAILED",
        invoice_id=invoice.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("error") == "Void invoices cannot be sent by email."


def test_invoice_send_by_email_reports_missing_resend_config(client, db_session, monkeypatch):
    import app.routes.invoices as invoices_routes

    invoice = _create_invoice_with_line(db_session, invoice_no="INV-RESEND-1")
    pdf_called = {"value": False}

    def _unexpected_render(*args, **kwargs):
        pdf_called["value"] = True
        return b"%PDF-1.4\n%unexpected\n"

    monkeypatch.setattr(invoices_routes, "render_invoice_pdf", _unexpected_render)

    response = client.post(
        f"/invoices/{invoice.id}/email",
        data={"to_email": "customer@example.com"},
    )

    assert response.status_code == 400
    assert "Resend API key is not configured." in response.text
    assert pdf_called["value"] is False

    audit_event = _invoice_email_audit_event(
        db_session,
        action="INVOICE_EMAIL_FAILED",
        invoice_id=invoice.id,
    )

    assert audit_event is not None
    assert audit_event.details_json.get("error") == "Resend API key is not configured."
