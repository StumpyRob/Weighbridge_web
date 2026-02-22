import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.config import settings
from app.models import (
    DirectionEnum,
    PrintJob,
    PrintProfile,
    PrintTemplate,
    PrintTemplateVersion,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)
import app.services.printing as printing_service
from app.templating import templates
from app.services.print_render import render_thermal


def _create_ticket(
    db_session,
    ticket_no: str = "T-PRINT-PROFILE-1",
    status: str = TicketStatusEnum.OPEN.value,
) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 2, 19, 12, 0, 0),
        status=status,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _minimal_print_payload(ticket_no: str) -> dict:
    return {
        "ticket_no": ticket_no,
        "datetime_display": "19/02/2026 12:00",
        "status": "OPEN",
        "direction": "INWARD",
        "transaction_type": "SALE",
        "walk_in_sale": False,
        "po_number": "",
        "vehicle": {"registration": ""},
        "customer": {"account_code": "", "name": ""},
        "product": {"code": "", "description": "", "unit_name": "", "unit_type": ""},
        "weights": {
            "gross_kg_display": "-",
            "tare_kg_display": "-",
            "net_kg_display": "-",
            "qty_display": "-",
            "unit_price_display": "-",
            "total_display": "-",
        },
        "logistics": {
            "haulier": "",
            "driver": "",
            "container": "",
            "destination": "",
            "carrier_licence_number": "",
        },
        "compliance": {
            "ewc_code_display": "",
            "ewc_code_6": "",
            "ewc_hazardous": False,
        },
    }


def test_print_profile_lookup_crud_basics(client, db_session):
    list_response = client.get("/admin/printing/profiles")
    assert list_response.status_code == 200

    create_response = client.post(
        "/admin/printing/profiles/new",
        data={
            "code": "THERMAL_SITE",
            "description": "Site thermal printer",
            "purpose": "TICKET_THERMAL",
            "template_name": "thermal_default.txt",
            "transport_mode": "NETWORK_RAW_9100",
            "transport_config": '{"host": "10.0.0.10", "port": 9100}',
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303

    profile = db_session.execute(
        select(PrintProfile).where(PrintProfile.code == "THERMAL_SITE")
    ).scalar_one()
    assert profile.transport_mode == "NETWORK_RAW_9100"
    assert profile.transport_config.get("host") == "10.0.0.10"
    assert profile.is_default is True

    update_response = client.post(
        f"/admin/printing/profiles/{profile.id}/edit",
        data={
            "code": "THERMAL_SITE",
            "description": "Updated profile",
            "purpose": "TICKET_THERMAL",
            "template_name": "thermal_default.txt",
            "transport_mode": "CUPS",
            "transport_config": '{"printer_name": "yard_thermal"}',
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303

    db_session.refresh(profile)
    assert profile.description == "Updated profile"
    assert profile.transport_mode == "CUPS"
    assert profile.transport_config.get("printer_name") == "yard_thermal"
    assert profile.is_default is False


def test_render_thermal_uses_override_dir_before_builtin(tmp_path, monkeypatch):
    override_dir = tmp_path / "print_templates"
    override_dir.mkdir()
    (override_dir / "thermal_default.txt").write_text(
        "OVERRIDE TICKET {{ payload.ticket_no }}",
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "print_template_override_dir", str(override_dir))
    rendered_override = render_thermal(_minimal_print_payload("T-OVERRIDE"))
    assert "OVERRIDE TICKET T-OVERRIDE" in rendered_override

    empty_override_dir = tmp_path / "empty_templates"
    empty_override_dir.mkdir()
    monkeypatch.setattr(settings, "print_template_override_dir", str(empty_override_dir))
    rendered_builtin = render_thermal(_minimal_print_payload("T-BUILTIN"))
    assert "WEIGHBRIDGE TICKET" in rendered_builtin


def test_ticket_print_post_requires_complete_ticket(client, db_session):
    ticket = _create_ticket(db_session, ticket_no="T-PRINT-REQUIRES-COMPLETE-1")
    profile = PrintProfile(
        code="THERMAL_REQUIRES_COMPLETE",
        description="Requires complete",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="CUPS",
        transport_config={"printer_name": "yard_thermal"},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert response.json()["error"] == "Ticket must be complete to print."


def test_ticket_print_post_returns_error_when_network_printing_disabled(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-DISABLED-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_NET_DISABLED",
        description="Thermal network profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="NETWORK_RAW_9100",
        transport_config={"host": "127.0.0.1", "port": 9100},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    before_status = ticket.status
    before_updated_at = ticket.updated_at

    monkeypatch.setattr(settings, "print_network_enabled", False)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "disabled" in response.json()["error"].lower()

    refreshed = db_session.get(Ticket, ticket.id)
    assert refreshed is not None
    assert refreshed.status == before_status
    assert refreshed.updated_at == before_updated_at


def test_ticket_print_post_validates_missing_network_host_without_state_change(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-NOHOST-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_NET_BADCFG",
        description="Thermal missing host",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="NETWORK_RAW_9100",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    before_status = ticket.status
    before_updated_at = ticket.updated_at

    monkeypatch.setattr(settings, "print_network_enabled", True)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "host" in response.json()["error"].lower()

    refreshed = db_session.get(Ticket, ticket.id)
    assert refreshed is not None
    assert refreshed.status == before_status
    assert refreshed.updated_at == before_updated_at


def test_ticket_print_post_succeeds_with_cups_profile(client, db_session, monkeypatch):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-CUPS-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_CUPS",
        description="Thermal via CUPS",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="CUPS",
        transport_config={"printer_name": "yard_thermal"},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    called: dict[str, object] = {}

    def _fake_send(job: bytes, mode: str, config: dict, **kwargs) -> None:
        called["job"] = job
        called["mode"] = mode
        called["config"] = config
        called["kwargs"] = kwargs

    monkeypatch.setattr(printing_service, "send_print_job", _fake_send)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["profile_code"] == "THERMAL_CUPS"
    assert called["mode"] == "cups"
    assert isinstance(called["job"], bytes)


def test_ticket_edit_shows_primary_print_button(client, db_session):
    ticket = _create_ticket(db_session, ticket_no="T-PRINT-UI-1")
    db_session.add(
        PrintProfile(
            code="THERMAL_UI",
            description="UI Thermal",
            purpose="TICKET_THERMAL",
            template_name="thermal_default.txt",
            transport_mode="CUPS",
            transport_config={"printer_name": "yard_thermal"},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert '<div class="actions btn-group ticket-header-actions">' in response.text
    primary_complete_button = re.search(
        r'(<button[^>]*class="btn btn--primary[^"]*"[^>]*name="action" value="complete"[^>]*>\s*Mark Complete\s*</button>)',
        response.text,
    )
    assert primary_complete_button is not None
    assert 'id="print-actions"' not in response.text
    assert "Preview (Browser Print)" not in response.text
    assert "Printer options" not in response.text
    assert "Printing available once ticket is complete." in response.text


def test_ticket_edit_complete_enables_print_and_preview(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-UI-COMPLETE-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    db_session.add(
        PrintProfile(
            code="THERMAL_UI_COMPLETE",
            description="UI Thermal Complete",
            purpose="TICKET_THERMAL",
            template_name="thermal_default.txt",
            transport_mode="CUPS",
            transport_config={"printer_name": "yard_thermal"},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert 'id="print-actions"' in response.text
    primary_print_button = re.search(
        r'(<button[^>]*class="btn btn--primary[^"]*"[^>]*>\s*Send to Printer\s*</button>)',
        response.text,
    )
    assert primary_print_button is not None
    assert "disabled" not in primary_print_button.group(1).lower()
    assert "Preview (Browser Print)" in response.text
    assert "Receipt Preview (WIP)" not in response.text
    assert "Printing available once ticket is complete." not in response.text
    assert "Printer options" in response.text
    assert 'name="action" value="complete"' not in response.text


def test_ticket_edit_advanced_selector_only_when_multiple_profiles_exist(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-UI-2",
        status=TicketStatusEnum.COMPLETE.value,
    )
    db_session.add(
        PrintProfile(
            code="THERMAL_SINGLE",
            description="Single thermal",
            purpose="TICKET_THERMAL",
            template_name="thermal_default.txt",
            transport_mode="CUPS",
            transport_config={"printer_name": "yard_thermal"},
            is_default=True,
            is_active=True,
        )
    )
    db_session.commit()

    single_response = client.get(f"/tickets/{ticket.id}")
    assert single_response.status_code == 200
    assert 'id="print_profile_id_thermal"' not in single_response.text

    db_session.add(
        PrintProfile(
            code="THERMAL_SECOND",
            description="Second thermal",
            purpose="TICKET_THERMAL",
            template_name="thermal_default.txt",
            transport_mode="CUPS",
            transport_config={"printer_name": "yard_thermal_2"},
            is_default=False,
            is_active=True,
        )
    )
    db_session.commit()

    multi_response = client.get(f"/tickets/{ticket.id}")
    assert multi_response.status_code == 200
    assert 'id="print_profile_id_thermal"' in multi_response.text


def test_ticket_print_without_profile_uses_default_thermal_and_shows_toast(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-DEFAULT-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    db_session.add_all(
        [
            PrintProfile(
                code="THERMAL_NON_DEFAULT",
                description="Thermal Secondary",
                purpose="TICKET_THERMAL",
                template_name="thermal_default.txt",
                transport_mode="CUPS",
                transport_config={"printer_name": "yard_thermal_secondary"},
                is_default=False,
                is_active=True,
            ),
            PrintProfile(
                code="THERMAL_DEFAULT_UI",
                description="Yard Thermal Default",
                purpose="TICKET_THERMAL",
                template_name="thermal_default.txt",
                transport_mode="CUPS",
                transport_config={"printer_name": "yard_thermal_main"},
                is_default=True,
                is_active=True,
            ),
            PrintProfile(
                code="A4_DEFAULT_UI",
                description="Yard A4 Default",
                purpose="TICKET_A4",
                template_name="a4_default.html",
                transport_mode="CUPS",
                transport_config={"printer_name": "yard_a4"},
                is_default=True,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    called: dict[str, object] = {}

    def _fake_send(job: bytes, mode: str, config: dict, **kwargs) -> None:
        called["job"] = job
        called["mode"] = mode
        called["config"] = config
        called["kwargs"] = kwargs

    monkeypatch.setattr(printing_service, "send_print_job", _fake_send)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Sent to printer: Yard Thermal Default" in response.text
    assert called["mode"] == "cups"
    assert called["config"] == {"printer_name": "yard_thermal_main"}


def test_legacy_lookup_print_profile_routes_redirect_to_admin(client):
    response = client.get("/lookups/print-profiles", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"].startswith("/admin/printing/profiles")


def test_print_template_can_be_created_and_selected_in_profile(client, db_session):
    template_response = client.post(
        "/admin/printing/templates/new",
        data={
            "code": "TMPL_THERMAL_TEST",
            "description": "Thermal test template",
            "purpose": "TICKET_THERMAL",
            "content_type": "TEXT",
            "content": "Template {{ payload.ticket_no }}",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert template_response.status_code == 303
    template = db_session.execute(
        select(PrintTemplate).where(PrintTemplate.code == "TMPL_THERMAL_TEST")
    ).scalar_one()

    profile_response = client.post(
        "/admin/printing/profiles/new",
        data={
            "code": "PROFILE_TMPL_LINK",
            "description": "Profile using template id",
            "purpose": "TICKET_THERMAL",
            "template_id": str(template.id),
            "template_name": "",
            "transport_mode": "LOCAL_BROWSER",
            "transport_config": "{}",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert profile_response.status_code == 303

    profile = db_session.execute(
        select(PrintProfile).where(PrintProfile.code == "PROFILE_TMPL_LINK")
    ).scalar_one()
    assert profile.template_id == template.id


def test_print_profile_preview_renders_text_template(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PREVIEW-TEXT-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_PREVIEW_TEXT",
        description="Preview template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Preview {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()
    profile = PrintProfile(
        code="PROFILE_PREVIEW_TEXT",
        description="Preview profile",
        purpose="TICKET_THERMAL",
        template_id=template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get(
        f"/admin/printing/profiles/{profile.id}/preview",
        params={"ticket_id": ticket.id},
    )
    assert response.status_code == 200
    assert "Preview T-PREVIEW-TEXT-1" in response.text
    assert "<pre" in response.text


def test_profile_test_print_creates_print_job_record(client, db_session):
    profile = PrintProfile(
        code="PROFILE_TEST_PRINT",
        description="Profile test print",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/admin/printing/profiles/{profile.id}/test-print",
        follow_redirects=False,
    )
    assert response.status_code == 303

    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.profile_id == profile.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is not None
    assert job.status == "SENT"
    assert job.transport_mode == "LOCAL_BROWSER"
    assert job.transport_config_json == {}
    assert job.attempt_count == 1


def test_ticket_print_dispatch_creates_print_job_record(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-JOB-THERMAL-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="PROFILE_TICKET_JOB",
        description="Profile ticket job",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.ticket_id == ticket.id, PrintJob.profile_id == profile.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is not None
    assert job.status == "SENT"
    assert job.transport_mode == "LOCAL_BROWSER"
    assert job.transport_config_json == {}


def test_template_save_blocks_invalid_render(client):
    response = client.post(
        "/admin/printing/templates/new",
        data={
            "code": "TMPL_BAD_RENDER",
            "description": "Bad template",
            "purpose": "TICKET_THERMAL",
            "content_type": "TEXT",
            "content": "{{ payload.ticket_no",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Template render failed" in response.text


def test_template_save_does_not_create_version_records(client, db_session):
    template = PrintTemplate(
        code="TMPL_NO_HISTORY_SAVE",
        description="No history save template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Version A {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    update_response = client.post(
        f"/admin/printing/templates/{template.id}/edit",
        data={
            "code": template.code,
            "description": template.description,
            "purpose": template.purpose,
            "content_type": template.content_type,
            "content": "Version B {{ payload.ticket_no }}",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303
    db_session.refresh(template)
    assert template.content.startswith("Version B")

    versions = db_session.execute(
        select(PrintTemplateVersion)
        .where(PrintTemplateVersion.template_id == template.id)
    ).scalars().all()
    assert versions == []


def test_print_template_form_shows_insert_field_dropdown(client):
    response = client.get("/admin/printing/templates/new")
    assert response.status_code == 200
    assert 'id="insert_field_token"' in response.text
    assert "Common tokens:" in response.text
    assert 'data-quick-token="{{ ticket.number }}"' in response.text
    assert 'data-quick-token="{{ customer.name }}"' in response.text
    assert 'data-quick-token="{{ weights.net_kg_display }}"' in response.text
    assert 'data-quick-token="{{ pricing.total_display }}"' in response.text
    assert "Some fields may be blank depending on ticket type." in response.text
    assert 'id="insert_default_ticket_layout"' in response.text
    assert "Insert Default Ticket Layout" in response.text
    assert 'id="default_ticket_layouts_json"' in response.text
    assert "Ticket fields" in response.text
    assert "{{ ticket.number }}" in response.text
    assert "{{ customer.name }}" in response.text
    assert "{{ weights.net_kg }}" in response.text
    assert "{{ pricing.total }}" in response.text


def test_print_template_save_accepts_operator_tokens(client, db_session):
    response = client.post(
        "/admin/printing/templates/new",
        data={
            "code": "TMPL_OPERATOR_TOKENS",
            "description": "Operator token template",
            "purpose": "TICKET_THERMAL",
            "content_type": "TEXT",
            "content": (
                "Ticket {{ ticket.number }}\n"
                "Customer {{ customer.name }}\n"
                "Net {{ weights.net_kg }}\n"
                "Total {{ pricing.total_display }}"
            ),
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    template = db_session.execute(
        select(PrintTemplate).where(PrintTemplate.code == "TMPL_OPERATOR_TOKENS")
    ).scalar_one_or_none()
    assert template is not None


def test_template_reset_to_default_restores_seeded_content(client, db_session):
    template = PrintTemplate(
        code="TMPL_RESET_DEFAULT",
        description="Reset template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Broken template {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.post(
        f"/admin/printing/templates/{template.id}/reset-default",
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(template)
    assert template.content.startswith("WEIGHBRIDGE TICKET")

    versions = db_session.execute(
        select(PrintTemplateVersion)
        .where(PrintTemplateVersion.template_id == template.id)
    ).scalars().all()
    assert versions == []


def test_template_rollback_endpoint_is_not_available(client, db_session):
    template = PrintTemplate(
        code="TMPL_NO_ROLLBACK",
        description="No rollback template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.post(
        f"/admin/printing/templates/{template.id}/rollback/1",
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_print_profile_duplicate_copies_transport_and_template(client, db_session):
    template = PrintTemplate(
        code="TMPL_DUP_PROFILE",
        description="Dup profile template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()
    profile = PrintProfile(
        code="PROFILE_DUP_SOURCE",
        description="Source profile",
        purpose="TICKET_THERMAL",
        template_id=template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_NODE_HTTP",
        transport_config={"url": "http://127.0.0.1:9123/print", "timeout_ms": 5000},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/admin/printing/profiles/{profile.id}/duplicate",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/admin/printing/profiles/" in response.headers["location"]
    assert "/edit" in response.headers["location"]

    duplicates = db_session.execute(
        select(PrintProfile)
        .where(PrintProfile.code.like("PROFILE_DUP_SOURCE_COPY%"))
        .order_by(PrintProfile.id.desc())
    ).scalars().all()
    assert duplicates
    duplicated = duplicates[0]
    assert duplicated.id != profile.id
    assert duplicated.purpose == profile.purpose
    assert duplicated.template_id == profile.template_id
    assert duplicated.transport_mode == profile.transport_mode
    assert duplicated.transport_config == profile.transport_config
    assert duplicated.is_default is False


def test_print_template_duplicate_copies_content_purpose_and_type(client, db_session):
    template = PrintTemplate(
        code="TMPL_DUP_SOURCE",
        description="Template duplicate source",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}\nNet {{ weights.net_kg_display }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.add(
        PrintTemplate(
            code="tmpl_dup_source_copy",
            description="Existing lower-case copy",
            purpose=template.purpose,
            content_type=template.content_type,
            content=template.content,
            is_active=True,
        )
    )
    db_session.commit()

    first_response = client.post(
        f"/admin/printing/templates/{template.id}/duplicate",
        follow_redirects=False,
    )
    assert first_response.status_code == 303
    first_location = first_response.headers["location"]
    assert "/admin/printing/templates/" in first_location
    assert "/edit" in first_location
    first_query = parse_qs(urlparse(first_location).query)
    assert first_query.get("duplicated") == ["1"]
    first_edit_page = client.get(first_location)
    assert first_edit_page.status_code == 200
    assert "Template duplicated. Update code and description, then save." in first_edit_page.text

    second_response = client.post(
        f"/admin/printing/templates/{template.id}/duplicate",
        follow_redirects=False,
    )
    assert second_response.status_code == 303

    duplicates = db_session.execute(
        select(PrintTemplate)
        .where(PrintTemplate.code.like("TMPL_DUP_SOURCE_COPY%"))
        .order_by(PrintTemplate.code.asc())
    ).scalars().all()
    assert duplicates
    codes = [item.code for item in duplicates]
    descriptions = [item.description for item in duplicates]
    assert "TMPL_DUP_SOURCE_COPY2" in codes
    assert "TMPL_DUP_SOURCE_COPY3" in codes
    assert "Template duplicate source (Copy)" in descriptions
    assert "Template duplicate source (Copy 2)" in descriptions
    for duplicated in duplicates:
        assert duplicated.id != template.id
        assert duplicated.purpose == template.purpose
        assert duplicated.content_type == template.content_type
        assert duplicated.content == template.content
        assert duplicated.is_active == template.is_active


def test_print_templates_list_shows_duplicate_action(client, db_session):
    template = PrintTemplate(
        code="TMPL_DUP_ACTION",
        description="Template duplicate action",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ ticket.number }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get("/admin/printing/templates")
    assert response.status_code == 200
    assert f"/admin/printing/templates/{template.id}/duplicate" in response.text


def test_ticket_edit_primary_print_dropdown_uses_default_profile(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-DROPDOWN-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    default_profile = PrintProfile(
        code="THERMAL_DROPDOWN_DEFAULT",
        description="Default dropdown profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    second_profile = PrintProfile(
        code="THERMAL_DROPDOWN_ALT",
        description="Alt dropdown profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=False,
        is_active=True,
    )
    db_session.add_all([default_profile, second_profile])
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")
    assert response.status_code == 200
    assert 'id="print_profile_id_primary"' in response.text
    assert f'value="{default_profile.id}" selected' in response.text
    assert "Default dropdown profile (Thermal)" in response.text


def test_ticket_print_failure_redirects_with_error_toast_and_job_id(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-FAIL-TOAST-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_FAIL_TOAST",
        description="Fail toast profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="CUPS",
        transport_config={"printer_name": "yard_thermal"},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    def _fail_send(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("Connection refused")

    monkeypatch.setattr(printing_service, "send_print_job", _fail_send)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        data={"profile_id": str(profile.id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Printer unreachable" in response.text
    assert "Job " in response.text

    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.ticket_id == ticket.id, PrintJob.profile_id == profile.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is not None
    assert job.status == "FAILED"


def test_print_last_ticket_again_replays_latest_successful_job(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-REPRINT-LAST-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_REPRINT_LAST",
        description="Reprint profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    first_print = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )
    assert first_print.status_code == 200
    before_count = db_session.execute(
        select(func.count(PrintJob.id)).where(PrintJob.ticket_id == ticket.id)
    ).scalar_one()

    replay = client.post("/tickets/print-last", follow_redirects=False)
    assert replay.status_code == 303
    query = parse_qs(urlparse(replay.headers["location"]).query)
    assert query.get("reprint_sent") == ["1"]
    assert query.get("reprint_job_id")
    assert query.get("reprint_ticket_no") == [ticket.ticket_no]
    job_id = int(query["reprint_job_id"][0])

    after_count = db_session.execute(
        select(func.count(PrintJob.id)).where(PrintJob.ticket_id == ticket.id)
    ).scalar_one()
    assert after_count == before_count + 1

    page = client.get(replay.headers["location"])
    assert page.status_code == 200
    assert f"Reprinted ticket {ticket.ticket_no} (Job {job_id})." in page.text
    assert f"/admin/printing/jobs/{job_id}" in page.text


def test_print_last_ticket_again_shows_error_when_no_successful_job(client):
    response = client.post("/tickets/print-last", follow_redirects=True)
    assert response.status_code == 200
    assert "No successful print job found to reprint." in response.text


def test_template_preview_renders_text_with_ticket_header(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-TMPL-PREVIEW-TEXT-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_PREVIEW_ROUTE_TEXT",
        description="Route text preview",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get(
        f"/admin/printing/templates/{template.id}/preview",
        params={"ticket_id": ticket.id},
    )
    assert response.status_code == 200
    assert "Previewing with Ticket: T-TMPL-PREVIEW-TEXT-1" in response.text
    assert "Ticket T-TMPL-PREVIEW-TEXT-1" in response.text
    assert "<pre" in response.text


def test_template_preview_renders_html_with_banner(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-TMPL-PREVIEW-HTML-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_PREVIEW_ROUTE_HTML",
        description="Route html preview",
        purpose="TICKET_A4",
        content_type="HTML",
        content="<html><body><h2>Ticket {{ payload.ticket_no }}</h2></body></html>",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get(
        f"/admin/printing/templates/{template.id}/preview",
        params={"ticket_id": ticket.id},
    )
    assert response.status_code == 200
    assert "Previewing with Ticket: <strong>T-TMPL-PREVIEW-HTML-1</strong>" in response.text
    assert "<h2>Ticket T-TMPL-PREVIEW-HTML-1</h2>" in response.text


def test_template_preview_shows_friendly_error_ui(client, db_session):
    template = PrintTemplate(
        code="TMPL_PREVIEW_ROUTE_ERROR",
        description="Route preview error",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="{{ payload.ticket_no",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get(f"/admin/printing/templates/{template.id}/preview")
    assert response.status_code == 400
    assert "Template render failed" in response.text
    assert "Internal Server Error" not in response.text


def test_template_preview_accepts_blank_ticket_id_query(client, db_session):
    template = PrintTemplate(
        code="TMPL_PREVIEW_BLANK_TICKET_ID",
        description="Route blank ticket id preview",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    response = client.get(
        f"/admin/printing/templates/{template.id}/preview",
        params={"ticket_id": ""},
    )
    assert response.status_code == 200
    assert "Previewing with Ticket:" in response.text
    assert "Input should be a valid integer" not in response.text


def test_template_form_shows_preview_controls(client, db_session):
    template = PrintTemplate(
        code="TMPL_PREVIEW_FORM_LINK",
        description="Preview form link",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    new_response = client.get("/admin/printing/templates/new")
    assert new_response.status_code == 200
    assert "Save template first to preview." in new_response.text

    edit_response = client.get(f"/admin/printing/templates/{template.id}/edit")
    assert edit_response.status_code == 200
    assert f"/admin/printing/templates/{template.id}/preview" in edit_response.text
    assert "printing-template-preview-ticket-id-help" in edit_response.text
    assert (
        "Enter a numeric ticket ID. If blank or not found, preview uses the latest completed ticket."
        in edit_response.text
    )


def test_profile_test_print_redirect_includes_job_link_data(client, db_session):
    profile = PrintProfile(
        code="PROFILE_TEST_PRINT_REDIRECT",
        description="Profile test print redirect",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={"host": "10.0.0.10", "port": 9100},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/admin/printing/profiles/{profile.id}/test-print",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query.get("print_sent") == ["1"]
    assert query.get("print_profile") == [profile.description]
    assert "print_job_id" in query

    job_id = int(query["print_job_id"][0])
    job = db_session.get(PrintJob, job_id)
    assert job is not None
    assert job.status == "SENT"

    page = client.get(location)
    assert page.status_code == 200
    assert f"Sent to printer: {profile.description} (Job {job_id})." in page.text
    assert f"/admin/printing/jobs/{job_id}" in page.text
    assert "View job" in page.text


def test_profile_test_print_failure_redirect_includes_job_id(
    client,
    db_session,
    monkeypatch,
):
    profile = PrintProfile(
        code="PROFILE_TEST_PRINT_FAIL",
        description="Profile test print fail",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="CUPS",
        transport_config={"printer_name": "yard_thermal"},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    def _fail_send(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("Connection refused")

    monkeypatch.setattr(printing_service, "send_print_job", _fail_send)
    response = client.post(
        f"/admin/printing/profiles/{profile.id}/test-print",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query.get("print_failed") == ["1"]
    assert "print_error" in query
    assert "print_job_id" in query
    assert query.get("print_profile") == [profile.description]

    job_id = int(query["print_job_id"][0])
    job = db_session.get(PrintJob, job_id)
    assert job is not None
    assert job.status == "FAILED"

    page = client.get(location)
    assert page.status_code == 200
    assert f"(Job {job_id})." in page.text
    assert f"/admin/printing/jobs/{job_id}" in page.text


def test_profiles_list_uses_compact_profile_column_and_actions_menu(client, db_session):
    profile = PrintProfile(
        code="PROFILE_LIST_TEST_BUTTON",
        description="Profile list button",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get("/admin/printing/profiles")
    assert response.status_code == 200
    assert '<th class="profiles-col-profile">Profile</th>' in response.text
    assert "profiles-col-code" not in response.text
    assert "profiles-col-description" not in response.text
    assert "printing-profile-code" in response.text
    assert "printing-profile-description" in response.text
    assert "printing-compact-pill" in response.text
    assert "printing-profile-actions-menu" in response.text
    assert f'aria-label="More actions for {profile.code}"' in response.text
    assert 'aria-haspopup="menu"' in response.text
    assert 'id="printing-profile-actions-menu-script"' in response.text
    assert "getBoundingClientRect" in response.text
    assert 'event.key !== "Escape"' in response.text
    assert 'window.addEventListener("resize"' in response.text
    assert '"scroll",' in response.text
    assert 'popover.style.position = "fixed"' in response.text
    assert f"/admin/printing/profiles/{profile.id}/test-print?return_to=list" in response.text
    assert "Send Test Ticket" in response.text


def test_profile_edit_hides_preview_ticket_frame_and_keeps_test_print_button(
    client,
    db_session,
):
    profile = PrintProfile(
        code="PROFILE_EDIT_TEST_BUTTON",
        description="Profile edit button",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get(f"/admin/printing/profiles/{profile.id}/edit")
    assert response.status_code == 200
    assert "Preview Ticket ID (optional)" not in response.text
    assert "printing-profile-preview-help" not in response.text
    assert "printing-profile-actions-help" not in response.text
    assert f'formaction="/admin/printing/profiles/{profile.id}/test-print"' in response.text
    assert "Send Test Ticket" in response.text
    assert "printing-send-test-ticket-help" in response.text


def test_ticket_print_success_sets_inline_status_and_longer_toast(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-INLINE-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_INLINE_STATUS",
        description="Inline Thermal",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="CUPS",
        transport_config={"printer_name": "yard_thermal"},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    def _fake_send(job: bytes, mode: str, config: dict, **kwargs) -> None:
        return None

    monkeypatch.setattr(printing_service, "send_print_job", _fake_send)

    response = client.post(
        f"/tickets/{ticket.id}/print",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert query.get("print_sent") == ["1"]
    assert query.get("print_profile") == [profile.description]
    assert query.get("print_status") == ["1"]
    assert "print_job_id" in query
    assert re.fullmatch(r"\d{2}:\d{2}", query.get("print_sent_at", [""])[0]) is not None

    job_id = int(query["print_job_id"][0])
    page = client.get(location)
    assert page.status_code == 200
    assert f"Sent to printer: {profile.description} (Job {job_id})." in page.text
    assert "Last print: Sent to printer" in page.text
    assert f"(Job #<code>{job_id}</code>)" in page.text
    assert f"/admin/printing/jobs/{job_id}" in page.text
    assert 'data-timeout-ms="7000"' in page.text


def test_print_jobs_list_shows_ticket_no_and_profile_columns(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-JOBS-COLUMNS-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_JOBS_COLUMNS",
        description="Jobs columns template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    profile = PrintProfile(
        code="THERMAL_JOBS_COLUMNS",
        description="Jobs list profile",
        purpose="TICKET_THERMAL",
        template_id=template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    print_response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )
    assert print_response.status_code == 200
    assert print_response.json()["ok"] is True

    response = client.get("/admin/printing/jobs")
    assert response.status_code == 200
    assert "Ticket No" in response.text
    assert "Profile" in response.text
    assert "Template" in response.text
    assert "Target" in response.text
    assert "Error summary" in response.text
    assert ticket.ticket_no in response.text
    assert profile.code in response.text
    assert profile.description in response.text
    assert template.code in response.text
    assert "Browser" in response.text


def test_print_profile_form_validates_typed_network_port(client):
    response = client.post(
        "/admin/printing/profiles/new",
        data={
            "code": "NETWORK_BAD_PORT",
            "description": "Bad port profile",
            "purpose": "TICKET_THERMAL",
            "template_name": "thermal_default.txt",
            "transport_mode": "NETWORK_RAW_9100",
            "raw_host": "10.0.0.10",
            "raw_port": "70000",
            "raw_timeout_seconds": "5",
            "transport_config": "{}",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Port must be a number between 1 and 65535." in response.text


def test_print_profile_form_validates_local_node_url(client):
    response = client.post(
        "/admin/printing/profiles/new",
        data={
            "code": "NODE_BAD_URL",
            "description": "Bad node url profile",
            "purpose": "TICKET_THERMAL",
            "template_name": "thermal_default.txt",
            "transport_mode": "LOCAL_NODE_HTTP",
            "node_url": "print-node",
            "node_timeout_ms": "5000",
            "transport_config": "{}",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "URL must be a valid http:// or https:// address." in response.text


def test_local_browser_profile_save_normalizes_transport_config(client, db_session):
    response = client.post(
        "/admin/printing/profiles/new",
        data={
            "code": "LOCAL_BROWSER_NORMALIZE",
            "description": "Local browser normalize",
            "purpose": "TICKET_THERMAL",
            "template_name": "thermal_default.txt",
            "transport_mode": "LOCAL_BROWSER",
            "transport_config": '{"host":"10.0.0.1","port":9100}',
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    profile = db_session.execute(
        select(PrintProfile).where(PrintProfile.code == "LOCAL_BROWSER_NORMALIZE")
    ).scalar_one()
    assert profile.transport_config == {}


def test_local_browser_job_detail_shows_clear_snapshot_message(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-JOB-DETAIL-BROWSER-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_JOB_DETAIL_BROWSER",
        description="Job detail browser",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={"host": "10.0.0.2", "port": 9100},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )
    assert response.status_code == 200
    job_id = int(response.json()["job_id"])

    detail = client.get(f"/admin/printing/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Browser-managed local print (no network transport config)." in detail.text


def test_ticket_print_post_local_browser_redirects_to_browser_print_page(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-BROWSER-REDIRECT-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_BROWSER_REDIRECT",
        description="Browser redirect profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.post(
        f"/tickets/{ticket.id}/print",
        data={"profile_id": str(profile.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/tickets/{ticket.id}/print/browser?")

    query = parse_qs(urlparse(location).query)
    assert query.get("profile_id") == [str(profile.id)]
    assert "job_id" in query
    assert query.get("printed_to") == ["Browser redirect profile"]

    job_id = int(query["job_id"][0])
    job = db_session.get(PrintJob, job_id)
    assert job is not None
    assert job.ticket_id == ticket.id
    assert job.profile_id == profile.id
    assert job.status == "SENT"


def test_ticket_browser_print_page_renders_text_in_pre_and_autoprints(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-BROWSER-TEXT-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_BROWSER_TEXT",
        description="Browser text template",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Browser print {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    profile = PrintProfile(
        code="THERMAL_BROWSER_TEXT",
        description="Browser text profile",
        purpose="TICKET_THERMAL",
        template_id=template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get(
        f"/tickets/{ticket.id}/print/browser",
        params={"profile_id": profile.id, "job_id": 123},
    )
    assert response.status_code == 200
    assert "<pre>" in response.text
    assert "Browser print T-BROWSER-TEXT-1" in response.text
    assert "window.print()" in response.text
    assert "class=\"site-header\"" not in response.text


def test_ticket_browser_print_preview_uses_selected_profile_template_and_default_fallback(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-BROWSER-PREVIEW-MARKER-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    default_template = PrintTemplate(
        code="TMPL_BROWSER_MARKER_DEFAULT",
        description="Default preview marker",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="DEFAULTMARKER-456 {{ payload.ticket_no }}",
        is_active=True,
    )
    selected_template = PrintTemplate(
        code="TMPL_BROWSER_MARKER_SELECTED",
        description="Selected preview marker",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="PREVIEWTEST-123 {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add_all([default_template, selected_template])
    db_session.flush()

    default_profile = PrintProfile(
        code="THERMAL_BROWSER_DEFAULT_MARKER",
        description="Default marker profile",
        purpose="TICKET_THERMAL",
        template_id=default_template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    selected_profile = PrintProfile(
        code="THERMAL_BROWSER_SELECTED_MARKER",
        description="Selected marker profile",
        purpose="TICKET_THERMAL",
        template_id=selected_template.id,
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=False,
        is_active=True,
    )
    db_session.add_all([default_profile, selected_profile])
    db_session.commit()

    selected_response = client.get(
        f"/tickets/{ticket.id}/print/browser",
        params={"profile_id": selected_profile.id},
    )
    assert selected_response.status_code == 200
    assert "PREVIEWTEST-123" in selected_response.text
    assert "DEFAULTMARKER-456" not in selected_response.text

    fallback_response = client.get(f"/tickets/{ticket.id}/print/browser")
    assert fallback_response.status_code == 200
    assert "DEFAULTMARKER-456" in fallback_response.text
    assert "PREVIEWTEST-123" not in fallback_response.text


def test_ticket_browser_print_page_renders_html_profile_content(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-BROWSER-HTML-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = PrintTemplate(
        code="TMPL_BROWSER_HTML",
        description="Browser html template",
        purpose="TICKET_A4",
        content_type="HTML",
        content="<section id='browser-html'>Ticket {{ payload.ticket_no }}</section>",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    profile = PrintProfile(
        code="THERMAL_BROWSER_HTML",
        description="Browser html profile",
        purpose="TICKET_A4",
        template_id=template.id,
        template_name="a4_default.html",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get(
        f"/tickets/{ticket.id}/print/browser",
        params={"profile_id": profile.id},
    )
    assert response.status_code == 200
    assert "id='browser-html'" in response.text or 'id="browser-html"' in response.text
    assert "Ticket T-BROWSER-HTML-1" in response.text
    assert "window.print()" in response.text
    assert "<pre>" not in response.text


def test_admin_printing_pages_show_profiles_only_collapsible_overview(client):
    profiles_page = client.get("/admin/printing/profiles")
    templates_page = client.get("/admin/printing/templates")
    jobs_page = client.get("/admin/printing/jobs")

    assert profiles_page.status_code == 200
    assert "How printing works" in profiles_page.text
    assert 'id="printing-overview-collapsible"' in profiles_page.text
    assert 'id="printing-overview-collapsible" open' not in profiles_page.text
    assert "Template</strong> = what the output looks like (TEXT/HTML)." in profiles_page.text
    assert "Profile</strong> = template + destination + purpose." in profiles_page.text
    assert "Send to Printer</strong> creates a <strong>Print Job</strong>." in profiles_page.text

    assert templates_page.status_code == 200
    assert "How printing works" not in templates_page.text
    assert 'id="printing-overview-collapsible"' not in templates_page.text

    assert jobs_page.status_code == 200
    assert "How printing works" not in jobs_page.text
    assert 'id="printing-overview-collapsible"' not in jobs_page.text


def test_admin_printing_direct_urls_land_on_the_matching_tab(client):
    profiles_page = client.get("/admin/printing/profiles")
    templates_page = client.get("/admin/printing/templates")
    jobs_page = client.get("/admin/printing/jobs")

    assert profiles_page.status_code == 200
    assert templates_page.status_code == 200
    assert jobs_page.status_code == 200

    assert (
        '<a class="btn btn--ghost is-active" href="/admin/printing/profiles">Profiles</a>'
        in profiles_page.text
    )
    assert (
        '<a class="btn btn--ghost is-active" href="/admin/printing/templates">Templates</a>'
        in templates_page.text
    )
    assert (
        '<a class="btn btn--ghost is-active" href="/admin/printing/jobs">Jobs</a>'
        in jobs_page.text
    )


def test_print_profile_form_has_tooltips_and_helper_lines(client):
    response = client.get("/admin/printing/profiles/new")
    assert response.status_code == 200

    assert "Profile Details" in response.text
    assert "Layout" in response.text
    assert "Destination" in response.text
    assert "Behaviour" in response.text

    assert "printing-profile-purpose-help" in response.text
    assert "printing-profile-template-help" in response.text
    assert "printing-profile-transport-mode-help" in response.text
    assert "printing-profile-transport-config-help" in response.text
    assert "printing-profile-default-help" in response.text
    assert "printing-profile-active-help" in response.text

    assert "What you are printing: Ticket Thermal, Ticket A4, Invoice A4, etc." in response.text
    assert "Defines layout/content. Change template to change formatting." in response.text
    assert "How the output is delivered: Browser, Network RAW 9100, Local Print Node." in response.text
    assert "Switch transport mode to show the right connection fields below." in response.text
    assert "Connection settings for the transport. Leave empty for Browser." in response.text
    assert "Used automatically when printing unless a different profile is selected." in response.text
    assert "Inactive profiles won&rsquo;t appear for operators." in response.text


def test_print_template_form_has_editor_tooltips(client, db_session):
    template = PrintTemplate(
        code="TMPL_HELP_TEXT",
        description="Template help text",
        purpose="TICKET_THERMAL",
        content_type="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()

    new_response = client.get("/admin/printing/templates/new")
    assert new_response.status_code == 200
    assert "printing-template-content-type-help" in new_response.text
    assert "printing-template-insert-token-help" in new_response.text
    assert "TEXT = best for 80mm thermal. HTML = best for A4 / styled layouts." in new_response.text
    assert "Common tokens:" in new_response.text
    assert "Some fields may be blank depending on ticket type." in new_response.text

    edit_response = client.get(f"/admin/printing/templates/{template.id}/edit")
    assert edit_response.status_code == 200
    assert "Restore Default Template" in edit_response.text
    assert "Restores the seeded default template content." not in edit_response.text
    assert "Version History" not in edit_response.text
    assert "/rollback/" not in edit_response.text


def test_jobs_pages_show_status_legend_and_diagnostic_tooltips(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-JOBS-HELP-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_JOBS_HELP",
        description="Jobs help profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    print_response = client.post(
        f"/tickets/{ticket.id}/print",
        json={"profile_code": profile.code},
    )
    assert print_response.status_code == 200
    job_id = int(print_response.json()["job_id"])

    jobs_list = client.get("/admin/printing/jobs")
    assert jobs_list.status_code == 200
    assert "Status legend" not in jobs_list.text
    assert "QUEUED</strong> = pending" in jobs_list.text
    assert "SENT</strong> = delivered" in jobs_list.text
    assert "FAILED</strong> = error / retry" in jobs_list.text
    assert "printing-jobs-status-help" in jobs_list.text

    detail = client.get(f"/admin/printing/jobs/{job_id}")
    assert detail.status_code == 200
    assert "printing-job-detail-retry-help" in detail.text
    assert "printing-job-detail-rendered-help" in detail.text
    assert "printing-job-detail-transport-help" in detail.text


def test_ticket_edit_complete_uses_send_to_printer_and_browser_preview_labels(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PRINT-LABELS-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_LABELS",
        description="Labels profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    response = client.get(f"/tickets/{ticket.id}")
    assert response.status_code == 200
    assert "Send to Printer" in response.text
    assert "Preview (Browser Print)" in response.text
    assert f'formaction="/tickets/{ticket.id}/print/browser"' in response.text
    assert 'id="preview_browser_print_button"' in response.text
    assert "Receipt Preview (WIP)" not in response.text
    assert f'href="/tickets/{ticket.id}/receipt"' not in response.text
    assert "ticket-receipt-wip-help" not in response.text
    assert "WIP: Receipt printing is not part of the current release scope." not in response.text
    assert ">Print<" not in response.text
    assert "Print preview" not in response.text


def test_ticket_edit_shows_receipt_wip_link_only_when_feature_enabled_in_dev_mode(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-RECEIPT-WIP-LINK-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    profile = PrintProfile(
        code="THERMAL_RECEIPT_WIP",
        description="Receipt WIP profile",
        purpose="TICKET_THERMAL",
        template_name="thermal_default.txt",
        transport_mode="LOCAL_BROWSER",
        transport_config={},
        is_default=True,
        is_active=True,
    )
    db_session.add(profile)
    db_session.commit()

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    monkeypatch.setattr(settings, "receipts_wip_enabled", True)
    templates.env.globals["DEV_MODE"] = True
    try:
        response = client.get(f"/tickets/{ticket.id}")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert response.status_code == 200
    assert "Receipt Preview (WIP)" in response.text
    assert f'href="/tickets/{ticket.id}/receipt"' in response.text
    assert "ticket-receipt-wip-help" in response.text
    assert "WIP: Receipt printing is not part of the current release scope." in response.text


def test_admin_printing_pages_show_receipts_wip_notice_when_enabled(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "receipts_wip_enabled", True)

    profiles_page = client.get("/admin/printing/profiles")
    templates_page = client.get("/admin/printing/templates")
    jobs_page = client.get("/admin/printing/jobs")

    assert profiles_page.status_code == 200
    assert "Receipt printing is currently enabled in WIP mode." in profiles_page.text

    assert templates_page.status_code == 200
    assert "Receipt printing is currently enabled in WIP mode." not in templates_page.text

    assert jobs_page.status_code == 200
    assert "Receipt printing is currently enabled in WIP mode." not in jobs_page.text
