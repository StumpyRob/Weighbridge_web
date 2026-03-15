import pytest
from sqlalchemy import func, select

from app.models import CompanySetting, PrintTemplate
from app.seed import force_refresh_system_print_templates
from app.services.print_render import render_from_content


@pytest.fixture()
def client(client_logged_in_no_setup):
    return client_logged_in_no_setup


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
    ("template_code", "sample_marker"),
    [
        ("TICKET_THERMAL_SYSTEM", "T-SAMPLE"),
        ("TICKET_A4_SYSTEM", "T-SAMPLE"),
        ("INVOICE_SYSTEM", "INV-SAMPLE"),
        ("WTN_SYSTEM", "WTN-T-SAMPLE"),
    ],
)
def test_system_template_preview_renders_with_sample_payload(
    client,
    db_session,
    template_code: str,
    sample_marker: str,
):
    force_refresh_system_print_templates(db_session)
    template = _system_template(db_session, template_code)

    response = client.get(f"/admin/printing/templates/{template.id}/preview")

    assert response.status_code == 200
    assert "Template render failed" not in response.text
    assert sample_marker in response.text


def test_invoice_system_template_preview_renders_logo_when_company_logo_is_set(
    client,
    db_session,
):
    force_refresh_system_print_templates(db_session)
    db_session.add(
        CompanySetting(
            name="System Template Logo Test",
            company_logo_path="https://example.com/logo.png",
        )
    )
    db_session.commit()

    template = _system_template(db_session, "INVOICE_SYSTEM")
    response = client.get(f"/admin/printing/templates/{template.id}/preview")

    assert response.status_code == 200
    assert "Template render failed" not in response.text
    assert "<img" in response.text
    assert "https://example.com/logo.png" in response.text


@pytest.mark.parametrize(
    ("template_code", "expected_class"),
    [
        ("TICKET_A4_SYSTEM", "status-open"),
        ("INVOICE_SYSTEM", "status-draft"),
    ],
)
def test_system_template_preview_uses_ui_status_palette(
    client,
    db_session,
    template_code: str,
    expected_class: str,
):
    force_refresh_system_print_templates(db_session)
    template = _system_template(db_session, template_code)

    response = client.get(f"/admin/printing/templates/{template.id}/preview")

    assert response.status_code == 200
    assert f'status-badge {expected_class}' in response.text


def test_render_from_content_normalizes_legacy_status_badges():
    legacy_invoice_html = render_from_content(
        {"status": "PAID"},
        (
            "<html><head><style>"
            ".status-badge { background: #fef3c7; border-radius: 30px; color: #92400e; display: inline-block; }"
            "</style></head><body><span class=\"status-badge\">PAID</span></body></html>"
        ),
    )
    assert 'status-badge status-paid' in legacy_invoice_html
    assert ".status-badge.status-paid" in legacy_invoice_html

    legacy_ticket_html = render_from_content(
        {"status": "COMPLETE"},
        (
            "<html><head><style>.meta { color: #334155; }</style></head><body>"
            "<div class=\"title-block\"><h1>TICKET</h1>"
            "<div class=\"meta\">No: T-COMP-1</div>"
            "<div class=\"meta\">Date: 01/01/2026</div>"
            "</div></body></html>"
        ),
    )
    assert "Status:" in legacy_ticket_html
    assert 'status-badge status-complete' in legacy_ticket_html
