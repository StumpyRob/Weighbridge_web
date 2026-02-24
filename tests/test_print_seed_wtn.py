from sqlalchemy import func, select

from app.models import PrintDestination, PrintTemplate
from app.seed import (
    PRINT_DELIVERY_LOCAL_BROWSER,
    PRINT_DOCUMENT_TYPE_INVOICE,
    PRINT_DOCUMENT_TYPE_TICKET,
    PRINT_DOCUMENT_TYPE_WTN,
    force_refresh_system_print_templates,
    seed_print_destinations,
    seed_print_templates,
)


def test_seed_print_templates_creates_default_wtn_template(db_session):
    seed_print_templates(db_session)

    template = db_session.execute(
        select(PrintTemplate).where(func.lower(PrintTemplate.code) == "wtn_system")
    ).scalar_one_or_none()

    assert template is not None
    assert template.document_type == PRINT_DOCUMENT_TYPE_WTN
    assert template.format == "HTML"
    assert bool(template.is_system) is True
    assert bool(template.is_active) is True
    assert "Waste Transfer Note" in str(template.content or "")


def test_seed_print_templates_creates_all_system_templates(db_session):
    seed_print_templates(db_session)

    rows = db_session.execute(
        select(PrintTemplate).where(PrintTemplate.is_system.is_(True))
    ).scalars().all()
    by_code = {str(row.code or "").upper(): row for row in rows}

    assert "TICKET_THERMAL_SYSTEM" in by_code
    assert "TICKET_A4_SYSTEM" in by_code
    assert "INVOICE_SYSTEM" in by_code
    assert "WTN_SYSTEM" in by_code

    assert by_code["TICKET_THERMAL_SYSTEM"].document_type == "TICKET"
    assert by_code["TICKET_THERMAL_SYSTEM"].format == "TEXT"
    assert by_code["TICKET_A4_SYSTEM"].document_type == "TICKET"
    assert by_code["TICKET_A4_SYSTEM"].format == "HTML"
    assert by_code["INVOICE_SYSTEM"].document_type == "INVOICE"
    assert by_code["INVOICE_SYSTEM"].format == "HTML"
    assert by_code["WTN_SYSTEM"].document_type == "WTN"
    assert by_code["WTN_SYSTEM"].format == "HTML"


def test_force_refresh_system_templates_overwrites_canonical_system_rows(db_session):
    stale_template = PrintTemplate(
        code="INVOICE_SYSTEM",
        description="Stale Invoice Template",
        document_type="INVOICE",
        format="HTML",
        content=(
            "<html><body>"
            "Legacy {{ invoice.invoice_no }} "
            "{{ payload.vehicle.registration }} "
            "{{ payload.logistics.haulier }}"
            "</body></html>"
        ),
        is_system=False,
        is_active=False,
    )
    db_session.add(stale_template)
    db_session.commit()

    force_refresh_system_print_templates(db_session)
    db_session.refresh(stale_template)

    assert stale_template.document_type == PRINT_DOCUMENT_TYPE_INVOICE
    assert stale_template.format == "HTML"
    assert bool(stale_template.is_system) is True
    assert bool(stale_template.is_active) is True
    content = str(stale_template.content or "")
    assert ("payload.logo_data_uri" in content) or ("p.logo_data_uri" in content)
    assert "invoice.invoice_no" not in content
    assert "payload.vehicle." not in content
    assert "payload.logistics." not in content
    assert "payload.weights." not in content
    assert ("payload.invoice_no" in content) or ("p.invoice_no" in content)


def test_seed_print_destinations_creates_default_wtn_destination(db_session):
    seed_print_templates(db_session)
    seed_print_destinations(db_session)

    destination = db_session.execute(
        select(PrintDestination).where(
            func.lower(PrintDestination.name) == "wtn default"
        )
    ).scalar_one_or_none()

    assert destination is not None
    assert destination.document_type == PRINT_DOCUMENT_TYPE_WTN
    assert destination.delivery_type == PRINT_DELIVERY_LOCAL_BROWSER
    assert bool(destination.is_default) is True
    assert bool(destination.is_active) is True

    template = db_session.get(PrintTemplate, destination.template_id)
    assert template is not None
    assert str(template.code or "").upper() == "WTN_SYSTEM"


def test_seed_printing_wtn_is_idempotent(db_session):
    seed_print_templates(db_session)
    seed_print_destinations(db_session)
    seed_print_templates(db_session)
    seed_print_destinations(db_session)

    template_count = db_session.execute(
        select(func.count(PrintTemplate.id)).where(
            func.lower(PrintTemplate.code) == "wtn_system"
        )
    ).scalar_one()
    destination_count = db_session.execute(
        select(func.count(PrintDestination.id)).where(
            func.lower(PrintDestination.name) == "wtn default"
        )
    ).scalar_one()

    assert int(template_count) == 1
    assert int(destination_count) == 1


def test_seed_print_destinations_skips_wtn_default_when_another_default_exists(db_session):
    custom_template = PrintTemplate(
        code="WTN_CUSTOM",
        description="Custom WTN template",
        document_type=PRINT_DOCUMENT_TYPE_WTN,
        format="HTML",
        content="<html><body>WTN CUSTOM</body></html>",
        is_active=True,
    )
    db_session.add(custom_template)
    db_session.flush()
    db_session.add(
        PrintDestination(
            name="Existing WTN Default",
            description="Existing WTN default",
            document_type=PRINT_DOCUMENT_TYPE_WTN,
            template_id=int(custom_template.id),
            delivery_type=PRINT_DELIVERY_LOCAL_BROWSER,
            delivery_config={},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    seed_print_templates(db_session)
    seed_print_destinations(db_session)

    existing_default = db_session.execute(
        select(PrintDestination).where(
            PrintDestination.document_type == PRINT_DOCUMENT_TYPE_WTN,
            PrintDestination.is_default.is_(True),
            PrintDestination.is_active.is_(True),
        )
    ).scalars().all()
    seeded_wtn_default = db_session.execute(
        select(PrintDestination).where(
            func.lower(PrintDestination.name) == "wtn default"
        )
    ).scalars().all()

    assert len(existing_default) == 1
    assert existing_default[0].name == "Existing WTN Default"
    assert seeded_wtn_default == []


def test_admin_printing_lists_show_seeded_wtn_rows(client, db_session):
    seed_print_templates(db_session)
    seed_print_destinations(db_session)

    templates_response = client.get("/admin/printing/templates?document_type=WTN")
    destinations_response = client.get("/admin/printing/destinations?document_type=WTN")

    assert templates_response.status_code == 200
    assert destinations_response.status_code == 200
    assert "WTN_SYSTEM" in templates_response.text
    assert "WTN Default" in destinations_response.text


def test_seed_print_destinations_creates_starter_defaults_for_all_document_types(db_session):
    seed_print_templates(db_session)
    seed_print_destinations(db_session)

    defaults = db_session.execute(
        select(PrintDestination).where(
            PrintDestination.is_default.is_(True),
            PrintDestination.is_active.is_(True),
        )
    ).scalars().all()
    defaults_by_type = {str(item.document_type or "").upper(): item for item in defaults}

    ticket_default = defaults_by_type.get(PRINT_DOCUMENT_TYPE_TICKET)
    invoice_default = defaults_by_type.get(PRINT_DOCUMENT_TYPE_INVOICE)
    wtn_default = defaults_by_type.get(PRINT_DOCUMENT_TYPE_WTN)

    assert ticket_default is not None
    assert invoice_default is not None
    assert wtn_default is not None

    ticket_template = db_session.get(PrintTemplate, int(ticket_default.template_id))
    invoice_template = db_session.get(PrintTemplate, int(invoice_default.template_id))
    wtn_template = db_session.get(PrintTemplate, int(wtn_default.template_id))

    assert ticket_default.delivery_type == PRINT_DELIVERY_LOCAL_BROWSER
    assert invoice_default.delivery_type == PRINT_DELIVERY_LOCAL_BROWSER
    assert wtn_default.delivery_type == PRINT_DELIVERY_LOCAL_BROWSER
    assert str(ticket_template.code or "").upper() == "TICKET_A4_SYSTEM"
    assert str(invoice_template.code or "").upper() == "INVOICE_SYSTEM"
    assert str(wtn_template.code or "").upper() == "WTN_SYSTEM"


def test_admin_template_preview_for_seeded_wtn_does_not_fail_on_missing_nested_keys(
    client,
    db_session,
):
    seed_print_templates(db_session)
    seed_print_destinations(db_session)
    template = db_session.execute(
        select(PrintTemplate).where(func.lower(PrintTemplate.code) == "wtn_system")
    ).scalar_one()

    response = client.get(f"/admin/printing/templates/{template.id}/preview")

    assert response.status_code == 200
    assert "Template render failed" not in response.text


def test_admin_template_preview_injects_shared_company_and_payload_context(
    client,
    db_session,
):
    ticket_template = PrintTemplate(
        code="TICKET_CTX_TEMPLATE",
        description="Ticket context template",
        document_type="TICKET",
        format="HTML",
        content=(
            "<html><body>TICKET_CTX {{ company_name }}|{{ company.name }}|"
            "{{ company_lines|length }}|{{ payload.ticket_no }}</body></html>"
        ),
        is_active=True,
    )
    invoice_template = PrintTemplate(
        code="INVOICE_CTX_TEMPLATE",
        description="Invoice context template",
        document_type="INVOICE",
        format="HTML",
        content=(
            "<html><body>INVOICE_CTX {{ company_name }}|{{ company.name }}|"
            "{{ company_lines|length }}|{{ payload.invoice_no }}</body></html>"
        ),
        is_active=True,
    )
    wtn_template = PrintTemplate(
        code="WTN_CTX_TEMPLATE",
        description="WTN context template",
        document_type="WTN",
        format="HTML",
        content=(
            "<html><body>WTN_CTX {{ company_name }}|{{ company.name }}|"
            "{{ company_lines|length }}|{{ payload.wtn_no }}</body></html>"
        ),
        is_active=True,
    )
    db_session.add_all([ticket_template, invoice_template, wtn_template])
    db_session.commit()
    db_session.refresh(ticket_template)
    db_session.refresh(invoice_template)
    db_session.refresh(wtn_template)

    ticket_response = client.get(
        f"/admin/printing/templates/{ticket_template.id}/preview"
    )
    invoice_response = client.get(
        f"/admin/printing/templates/{invoice_template.id}/preview"
    )
    wtn_response = client.get(f"/admin/printing/templates/{wtn_template.id}/preview")

    assert ticket_response.status_code == 200
    assert invoice_response.status_code == 200
    assert wtn_response.status_code == 200

    assert "TICKET_CTX Your Company Name|Your Company Name|" in ticket_response.text
    assert "|T-SAMPLE" in ticket_response.text

    assert "INVOICE_CTX Your Company Name|Your Company Name|" in invoice_response.text
    assert "|INV-SAMPLE" in invoice_response.text

    assert "WTN_CTX Your Company Name|Your Company Name|" in wtn_response.text
    assert "|WTN-T-SAMPLE" in wtn_response.text
