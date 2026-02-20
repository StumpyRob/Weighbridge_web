import re
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.models import DirectionEnum, PrintProfile, Ticket, TicketStatusEnum, TransactionTypeEnum
import app.routes.tickets as tickets_routes
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
    list_response = client.get("/lookups/print-profiles")
    assert list_response.status_code == 200

    create_response = client.post(
        "/lookups/print-profiles/new",
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
        f"/lookups/print-profiles/{profile.id}/edit",
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

    def _fake_send(job: bytes, mode: str, config: dict) -> None:
        called["job"] = job
        called["mode"] = mode
        called["config"] = config

    monkeypatch.setattr(tickets_routes, "send_print_job", _fake_send)
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
    assert "Print preview" not in response.text
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
        r'(<button[^>]*class="btn btn--primary[^"]*"[^>]*>\s*Print\s*</button>)',
        response.text,
    )
    assert primary_print_button is not None
    assert "disabled" not in primary_print_button.group(1).lower()
    assert "Print preview" in response.text
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

    def _fake_send(job: bytes, mode: str, config: dict) -> None:
        called["job"] = job
        called["mode"] = mode
        called["config"] = config

    monkeypatch.setattr(tickets_routes, "send_print_job", _fake_send)
    response = client.post(
        f"/tickets/{ticket.id}/print",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Sent to printer: Yard Thermal Default" in response.text
    assert called["mode"] == "cups"
    assert called["config"] == {"printer_name": "yard_thermal_main"}
