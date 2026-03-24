from datetime import datetime
import sys
import types
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.models import (
    DirectionEnum,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)
from app.seed import force_refresh_system_print_templates
import app.services.pdf as pdf_service
from app.services.pdf import (
    SINGLE_PAGE_TEMPLATE_ERROR,
    count_html_pages,
    prepare_html_for_print_output,
)
from app.services.print_payload import build_print_payload
from app.services.print_render import render_from_content
from app.services.printing import (
    DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
    DOCUMENT_TYPE_INVOICE,
    DOCUMENT_TYPE_TICKET,
    DOCUMENT_TYPE_WTN,
    PRINT_CONTENT_TYPE_TEXT,
    execute_rendered_print,
)


def _system_template(db_session, code: str) -> PrintTemplate:
    template = (
        db_session.execute(
            select(PrintTemplate).where(func.lower(PrintTemplate.code) == code.lower())
        )
        .scalars()
        .first()
    )
    assert template is not None, f"Missing system template: {code}"
    return template


@pytest.mark.parametrize(
    ("template_code", "document_type"),
    [
        ("TICKET_A4_SYSTEM", DOCUMENT_TYPE_TICKET),
        ("INVOICE_SYSTEM", DOCUMENT_TYPE_INVOICE),
        ("WTN_SYSTEM", DOCUMENT_TYPE_WTN),
    ],
)
def test_system_html_templates_render_as_single_page(
    db_session,
    template_code: str,
    document_type: str,
):
    force_refresh_system_print_templates(db_session)
    template = _system_template(db_session, template_code)

    payload = build_print_payload(db_session, document_type)
    rendered_html = render_from_content(payload, template.content, db=db_session)
    prepared_html = prepare_html_for_print_output(rendered_html)

    assert count_html_pages(prepared_html) == 1


def test_system_wtn_template_with_long_values_stays_single_page(db_session):
    force_refresh_system_print_templates(db_session)
    template = _system_template(db_session, "WTN_SYSTEM")

    payload = build_print_payload(db_session, DOCUMENT_TYPE_WTN)
    payload.update(
        {
            "customer_name": "North Yard Waste Recovery and Environmental Services Limited",
            "haulier_name": "Regional Haulage and Transfer Operations Group",
            "carrier_name": "Regional Haulage and Transfer Operations Group",
            "waste_description": (
                "Mixed construction and demolition waste including timber, plasterboard, "
                "plastics, metals, packaging residues, and inert fines from sorting operations"
            ),
            "ewc_description": (
                "Mixed construction and demolition waste from transfer station inspection and load review"
            ),
            "origin_site": "North Transfer Station, Building 4, Riverside Industrial Estate",
            "destination_site": "Materials Recovery Facility, South Dock Environmental Campus",
            "producer_signature_signer_name": "Alexandra Thompson",
            "carrier_signature_signer_name": "Christopher Morgan",
            "receiver_signature_signer_name": "Patricia Wilkinson",
        }
    )

    rendered_html = render_from_content(payload, template.content, db=db_session)
    prepared_html = prepare_html_for_print_output(rendered_html)

    assert count_html_pages(prepared_html) == 1


def test_system_ticket_template_with_long_values_stays_single_page(db_session):
    force_refresh_system_print_templates(db_session)
    template = _system_template(db_session, "TICKET_A4_SYSTEM")

    payload = build_print_payload(db_session, DOCUMENT_TYPE_TICKET)
    payload.update(
        {
            "customer_name": "North Ridge Civil Engineering and Environmental Recovery Limited",
            "customer_account_code": "NRCE-REC-204",
            "driver_name": "Christopher Alexander Morgan",
            "haulier_name": "Regional Bulk Logistics and Aggregates Transport Services",
            "destination_name": "Materials Recovery Centre, Riverside Industrial Estate",
            "origin_site": "Main Transfer Yard, North Compound",
            "product_code": "P-REC-001",
            "product_description": (
                "Recovered aggregate blend for general construction and site reinstatement works"
            ),
            "waste_description": (
                "Mixed inert recovery material screened from construction and demolition arisings"
            ),
            "ewc_code": "17 09 04",
            "ewc_description": "Mixed construction and demolition waste",
        }
    )

    rendered_html = render_from_content(payload, template.content, db=db_session)
    prepared_html = prepare_html_for_print_output(rendered_html)

    assert count_html_pages(prepared_html) == 1


def test_print_safe_policy_blocks_background_images():
    html = (
        "<html><head><style>"
        ".card{background-image:url('watermark.png');}"
        "</style></head><body><div class='card'>x</div></body></html>"
    )

    prepared_html = prepare_html_for_print_output(html)

    assert "background-image:url('watermark.png')" in prepared_html
    assert "background-image: none !important;" in prepared_html


def test_print_safe_policy_blocks_shadow_and_effect_rules():
    html = (
        "<html><head><style>"
        ".card{box-shadow:0 0 12px #000;text-shadow:1px 1px #000;filter:blur(2px);backdrop-filter:blur(3px);}"
        "</style></head><body><div class='card'>x</div></body></html>"
    )

    prepared_html = prepare_html_for_print_output(html)

    assert "box-shadow: none !important;" in prepared_html
    assert "text-shadow: none !important;" in prepared_html
    assert "filter: none !important;" in prepared_html
    assert "backdrop-filter: none !important;" in prepared_html


def test_print_safe_policy_preserves_background_color_styles():
    html = (
        "<html><head><style>"
        ".card{background-color:#eef6ff;border:1px solid #cbd5e1;}"
        "</style></head><body><div class='card'>x</div></body></html>"
    )

    prepared_html = prepare_html_for_print_output(html)

    assert "background-color:#eef6ff" in prepared_html
    assert "background-color: transparent" not in prepared_html


def _install_fake_weasyprint(monkeypatch, *, requested_url: str):
    class _FakeDocument:
        pages = [object()]

        def write_pdf(self):
            return b"%PDF-1.4\n%fake\n"

    class _FakeHTML:
        def __init__(self, string, base_url=None, url_fetcher=None):
            _ = (string, base_url)
            self.url_fetcher = url_fetcher

        def render(self):
            if self.url_fetcher is None:
                raise AssertionError("url_fetcher is required for policy enforcement.")
            self.url_fetcher(requested_url)
            return _FakeDocument()

        def write_pdf(self):
            return _FakeDocument().write_pdf()

    fake_module = types.ModuleType("weasyprint")
    fake_module.HTML = _FakeHTML
    fake_module.default_url_fetcher = lambda _url, *_args, **_kwargs: {"string": b""}
    monkeypatch.setitem(sys.modules, "weasyprint", fake_module)


def test_pdf_renderer_blocks_external_http_resources(monkeypatch):
    _install_fake_weasyprint(
        monkeypatch,
        requested_url="https://fonts.googleapis.com/css2?family=Inter",
    )
    monkeypatch.setattr(
        pdf_service,
        "check_invoice_pdf_renderer",
        lambda: pdf_service.PdfRendererStatus(available=True, detail=None),
    )
    monkeypatch.setattr(pdf_service, "_configure_windows_weasyprint_dlls", lambda: None)

    with pytest.raises(RuntimeError, match="External resource blocked"):
        pdf_service.render_html_pdf_bytes(
            "<html><body><p>Sample</p></body></html>",
            base_url="https://app.example.com",
        )


def test_pdf_renderer_allows_same_host_static_assets(monkeypatch):
    _install_fake_weasyprint(
        monkeypatch,
        requested_url="https://app.example.com/static/uploads/company/logo.png",
    )
    monkeypatch.setattr(
        pdf_service,
        "check_invoice_pdf_renderer",
        lambda: pdf_service.PdfRendererStatus(available=True, detail=None),
    )
    monkeypatch.setattr(pdf_service, "_configure_windows_weasyprint_dlls", lambda: None)

    pdf_bytes = pdf_service.render_html_pdf_bytes(
        "<html><body><p>Sample</p></body></html>",
        base_url="https://app.example.com",
    )

    assert pdf_bytes.startswith(b"%PDF")


def test_ticket_send_marks_job_failed_when_template_exceeds_single_page(client, db_session):
    ticket = Ticket(
        ticket_no="T-OVERSIZE-SEND-1",
        datetime=datetime(2026, 2, 24, 10, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.flush()

    template = PrintTemplate(
        code="TEMPLATE_OVERSIZE_SEND",
        description="Oversized Send Template",
        document_type=DOCUMENT_TYPE_TICKET,
        format="HTML",
        content=(
            "<html><body>"
            + "".join(f"<div>Oversized send row {index:04d}</div>" for index in range(1200))
            + "</body></html>"
        ),
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name="A Oversize Send Destination",
        description="A Oversize Send Destination",
        document_type=DOCUMENT_TYPE_TICKET,
        template_id=template.id,
        delivery_type=DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        delivery_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()

    response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}?")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("print_failed", [""])[0] == "1"
    assert SINGLE_PAGE_TEMPLATE_ERROR in query.get("print_error_detail", [""])[0]

    failed_job = (
        db_session.execute(
            select(PrintJob)
            .where(
                PrintJob.ticket_id == ticket.id,
                PrintJob.document_type == DOCUMENT_TYPE_TICKET,
            )
            .order_by(PrintJob.id.desc())
        )
        .scalars()
        .first()
    )
    assert failed_job is not None
    assert failed_job.status == "FAILED"
    assert SINGLE_PAGE_TEMPLATE_ERROR in str(failed_job.last_error or "")


def test_text_templates_bypass_single_page_and_print_safe_enforcement(db_session):
    oversized_text = "\n".join(f"Line {index:04d}" for index in range(5000))
    result = execute_rendered_print(
        db_session,
        document_type=DOCUMENT_TYPE_TICKET,
        rendered_content=oversized_text,
        content_type=PRINT_CONTENT_TYPE_TEXT,
        delivery_type=DELIVERY_TYPE_PRINT_LOCAL_BROWSER,
        delivery_config={},
    )

    assert result.job.status == "SENT"
    assert result.browser_content == oversized_text
