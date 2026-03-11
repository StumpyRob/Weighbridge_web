from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

import app.services.printing as printing_service
from app.models import Customer, Invoice, InvoiceLine, PrintDestination, PrintJob, PrintTemplate
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


def _create_invoice_with_line(db_session, *, invoice_no: str = "INV-PRINT-1") -> Invoice:
    customer = Customer(
        account_code=f"AC-{invoice_no}",
        name=f"Customer {invoice_no}",
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
        status="DRAFT",
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


def test_invoice_detail_documents_frame_groups_invoice_actions(client, db_session):
    invoice = _create_invoice_with_line(db_session, invoice_no="INV-UI-1")
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
    assert "ticket-header-actions" not in response.text
    assert "Preview" in response.text
    assert "Download PDF" in response.text
    assert "Print" in response.text
    assert (
        "Browser printing may add URL/date/time headers/footers depending on your browser settings."
        not in response.text
    )
    assert response.text.index("Documents") < response.text.index("Summary")
    assert "Advanced printing" not in response.text
    assert "Preview Invoice" not in response.text
    assert "Print locally (browser)" not in response.text


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
