import pytest
from sqlalchemy import func, select

from app.models import CompanySetting, PrintTemplate
from app.seed import force_refresh_system_print_templates


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
            logo_url="https://example.com/logo.png",
        )
    )
    db_session.commit()

    template = _system_template(db_session, "INVOICE_SYSTEM")
    response = client.get(f"/admin/printing/templates/{template.id}/preview")

    assert response.status_code == 200
    assert "Template render failed" not in response.text
    assert "<img" in response.text
    assert "https://example.com/logo.png" in response.text
