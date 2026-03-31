from datetime import datetime
import base64
from urllib.parse import parse_qs, urlparse
import uuid

from sqlalchemy import select

import app.services.printing as printing_service
from app.models import (
    DirectionEnum,
    PrintAgent,
    PrintAgentPairing,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    User,
)
from app.services.print_agents import hash_print_agent_key
from app.services.print_agents import hash_print_agent_pairing_code
from app.templating import templates


def _create_ticket(
    db_session,
    *,
    ticket_no: str = "T-PRINT-1",
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


def _create_template(
    db_session,
    *,
    code: str,
    document_type: str,
    template_format: str,
    content: str,
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
    document_type: str,
    template_id: int,
    delivery_type: str,
    delivery_config: dict,
    is_default: bool = True,
) -> PrintDestination:
    destination = PrintDestination(
        name=name,
        description=name,
        document_type=document_type,
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


def test_admin_printing_root_redirects_to_destinations(client):
    response = client.get("/admin/printing", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/printing/destinations"


def test_admin_print_agents_page_loads(client):
    response = client.get("/admin/printing/agents")

    assert response.status_code == 200
    assert "Print Agents" in response.text
    assert "Complete Pairing" in response.text
    assert "Pending Pairings" in response.text


def test_admin_print_agents_page_shows_pending_pairings(client):
    pairing_request = client.post(
        "/api/print/agents/pairing/request",
        json={"name": "Reception Agent"},
    )
    assert pairing_request.status_code == 200

    response = client.get("/admin/printing/agents")

    assert response.status_code == 200
    assert "Reception Agent" in response.text
    assert "Pending" in response.text


def test_admin_print_agents_page_shows_paired_agents(client, db_session):
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Office Agent",
        api_key=hash_print_agent_key("office-agent-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    response = client.get("/admin/printing/agents")

    assert response.status_code == 200
    assert "Office Agent" in response.text
    assert agent.id in response.text
    assert "Offline" in response.text


def test_admin_print_agents_page_shows_printer_sync_summary(client, db_session):
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Office Agent",
        api_key=hash_print_agent_key("office-agent-printer-key"),
        status="OFFLINE",
        printers_json=[
            {
                "name": "Zebra ZSB-DP14",
                "is_default": False,
                "is_online": True,
            },
            {
                "name": "Microsoft Print to PDF",
                "is_default": True,
                "is_online": True,
            },
        ],
        printers_synced_at=datetime(2026, 3, 31, 10, 15, 0),
    )
    db_session.add(agent)
    db_session.commit()

    response = client.get("/admin/printing/agents")

    assert response.status_code == 200
    assert "2 known" in response.text
    assert "31/03/2026 10:15:00" in response.text
    assert "Zebra ZSB-DP14" in response.text
    assert "Microsoft Print to PDF (default, online)" in response.text


def test_admin_print_agents_pair_valid_code_works(client, db_session):
    pairing_request = client.post(
        "/api/print/agents/pairing/request",
        json={"name": "Local Agent"},
    )
    assert pairing_request.status_code == 200
    pairing_payload = pairing_request.json()

    response = client.post(
        "/admin/printing/agents/pair",
        data={
            "pairing_code": pairing_payload["pairing_code"],
            "agent_name": "Friendly Agent",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Paired print agent Friendly Agent." in response.text
    assert "Friendly Agent" in response.text

    destination_agent = db_session.execute(
        select(PrintAgent).where(PrintAgent.name == "Friendly Agent")
    ).scalars().first()
    assert destination_agent is not None


def test_admin_print_agents_pair_invalid_code_fails_cleanly(client):
    response = client.post(
        "/admin/printing/agents/pair",
        data={"pairing_code": "BAD-CODE"},
        follow_redirects=True,
    )

    assert response.status_code == 400
    assert "Print agent pairing code was not found." in response.text


def test_admin_print_agents_pair_expired_code_fails_cleanly(client, db_session):
    pairing_request = client.post(
        "/api/print/agents/pairing/request",
        json={"name": "Expiring Agent"},
    )
    assert pairing_request.status_code == 200
    pairing_payload = pairing_request.json()

    pairing = db_session.execute(
        select(PrintAgentPairing).where(PrintAgentPairing.id == pairing_payload["pairing_id"])
    ).scalars().first()
    assert pairing is not None
    pairing.expires_at = datetime(2026, 3, 29, 11, 0, 0)
    db_session.commit()

    response = client.post(
        "/admin/printing/agents/pair",
        data={"pairing_code": pairing_payload["pairing_code"]},
        follow_redirects=True,
    )

    assert response.status_code == 410
    assert "Print agent pairing code has expired." in response.text


def test_admin_print_agents_can_cancel_pending_pairing(client, db_session):
    pairing_request = client.post(
        "/api/print/agents/pairing/request",
        json={"name": "Cancel Me"},
    )
    assert pairing_request.status_code == 200
    pairing_payload = pairing_request.json()

    response = client.post(
        f"/admin/printing/agents/pairings/{pairing_payload['pairing_id']}/cancel",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Canceled print agent pairing Cancel Me." in response.text
    pairing = db_session.execute(
        select(PrintAgentPairing).where(
            PrintAgentPairing.id == pairing_payload["pairing_id"]
        )
    ).scalars().first()
    assert pairing is None


def test_admin_print_agents_canceled_pairing_no_longer_appears(client):
    pairing_request = client.post(
        "/api/print/agents/pairing/request",
        json={"name": "Stale Pairing"},
    )
    assert pairing_request.status_code == 200
    pairing_payload = pairing_request.json()

    response = client.post(
        f"/admin/printing/agents/pairings/{pairing_payload['pairing_id']}/cancel",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "No pending pairings." in response.text


def test_admin_print_agents_can_revoke_unassigned_agent(client, db_session):
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Old Yard Agent",
        api_key=hash_print_agent_key("old-yard-agent-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    response = client.post(
        f"/admin/printing/agents/{agent.id}/revoke",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Revoked print agent Old Yard Agent." in response.text
    db_session.refresh(agent)
    assert agent.status == "REVOKED"

    heartbeat = client.post(
        "/api/print/agents/heartbeat",
        headers={"X-Agent-Key": "old-yard-agent-key"},
    )
    assert heartbeat.status_code == 401

    page = client.get("/admin/printing/agents")
    assert page.status_code == 200
    assert "Old Yard Agent" not in page.text


def test_admin_print_agents_show_revoked_filter_includes_revoked_agents(client, db_session):
    active_agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Active Yard Agent",
        api_key=hash_print_agent_key("active-yard-agent-key"),
        status="OFFLINE",
    )
    revoked_agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Revoked Yard Agent",
        api_key=hash_print_agent_key("revoked-yard-agent-key"),
        status="REVOKED",
    )
    db_session.add(active_agent)
    db_session.add(revoked_agent)
    db_session.commit()

    hidden_response = client.get("/admin/printing/agents")
    shown_response = client.get("/admin/printing/agents?show_revoked=1")

    assert hidden_response.status_code == 200
    assert "Active Yard Agent" in hidden_response.text
    assert "Revoked Yard Agent" not in hidden_response.text
    assert shown_response.status_code == 200
    assert "Active Yard Agent" in shown_response.text
    assert "Revoked Yard Agent" in shown_response.text


def test_admin_print_agents_bulk_cancel_expired_pending_pairings_only_affects_expired_pending(
    client,
    db_session,
):
    expired_pending = PrintAgentPairing(
        id=str(uuid.uuid4()),
        requested_name="Expired Pending",
        paired_name=None,
        pairing_code_hash=hash_print_agent_pairing_code("EXPD-PEND"),
        exchange_token_hash=hash_print_agent_key("expired-pending-token"),
        status="PENDING",
        expires_at=datetime(2000, 3, 29, 11, 0, 0),
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    current_pending = PrintAgentPairing(
        id=str(uuid.uuid4()),
        requested_name="Current Pending",
        paired_name=None,
        pairing_code_hash=hash_print_agent_pairing_code("CURR-PEND"),
        exchange_token_hash=hash_print_agent_key("current-pending-token"),
        status="PENDING",
        expires_at=datetime(2099, 3, 30, 13, 0, 0),
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    expired_paired = PrintAgentPairing(
        id=str(uuid.uuid4()),
        requested_name="Expired Paired",
        paired_name="Expired Paired",
        pairing_code_hash=hash_print_agent_pairing_code("EXPD-PAIR"),
        exchange_token_hash=hash_print_agent_key("expired-paired-token"),
        status="PAIRED",
        expires_at=datetime(2000, 3, 29, 11, 0, 0),
        paired_at=datetime(2026, 3, 29, 10, 0, 0),
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    db_session.add(expired_pending)
    db_session.add(current_pending)
    db_session.add(expired_paired)
    db_session.commit()
    expired_pending_id = expired_pending.id
    current_pending_id = current_pending.id
    expired_paired_id = expired_paired.id

    response = client.post(
        "/admin/printing/agents/pairings/cancel-expired",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Canceled 1 expired pending print agent pairing(s)." in response.text

    db_session.expire_all()
    assert db_session.get(PrintAgentPairing, expired_pending_id) is None
    assert db_session.get(PrintAgentPairing, current_pending_id) is not None
    assert db_session.get(PrintAgentPairing, expired_paired_id) is not None


def test_admin_print_agents_bulk_cancel_expired_pending_pairings_is_tenant_scoped(
    client,
    db_session,
):
    current_tenant_expired = PrintAgentPairing(
        id=str(uuid.uuid4()),
        requested_name="Current Tenant Expired",
        paired_name=None,
        pairing_code_hash=hash_print_agent_pairing_code("CURR-EXPD"),
        exchange_token_hash=hash_print_agent_key("curr-expired-token"),
        status="PENDING",
        expires_at=datetime(2000, 3, 29, 11, 0, 0),
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    other_tenant_expired = PrintAgentPairing(
        id=str(uuid.uuid4()),
        tenant_id=2,
        requested_name="Other Tenant Expired",
        paired_name=None,
        pairing_code_hash=hash_print_agent_pairing_code("OTHR-EXPD"),
        exchange_token_hash=hash_print_agent_key("other-expired-token"),
        status="PENDING",
        expires_at=datetime(2000, 3, 29, 11, 0, 0),
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    db_session.add(current_tenant_expired)
    db_session.add(other_tenant_expired)
    db_session.commit()
    current_tenant_expired_id = current_tenant_expired.id
    other_tenant_expired_id = other_tenant_expired.id

    response = client.post(
        "/admin/printing/agents/pairings/cancel-expired",
        follow_redirects=True,
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(PrintAgentPairing, current_tenant_expired_id) is None
    other_tenant_expired = db_session.get(PrintAgentPairing, other_tenant_expired_id)
    assert other_tenant_expired is not None
    assert other_tenant_expired.status == "PENDING"


def test_admin_print_agents_revoke_assigned_agent_is_blocked(client, db_session):
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Assigned Agent",
        api_key=hash_print_agent_key("assigned-agent-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    template = _create_template(
        db_session,
        code="ASSIGNED_PULL_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PULL {{ payload.ticket_no }}",
    )
    _create_destination(
        db_session,
        name="Assigned Pull Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config={
            "agent_id": agent.id,
            "printer_name": "Assigned Printer",
            "copies": 1,
        },
        is_default=False,
    )

    response = client.post(
        f"/admin/printing/agents/{agent.id}/revoke",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Cannot revoke print agent Assigned Agent while assigned to PRINT_AGENT_PULL destination" in response.text
    db_session.refresh(agent)
    assert agent.status == "OFFLINE"


def test_admin_print_agents_cleanup_actions_are_tenant_scoped(client, db_session):
    other_pairing = PrintAgentPairing(
        id=str(uuid.uuid4()),
        tenant_id=2,
        requested_name="Other Tenant Pairing",
        paired_name=None,
        pairing_code_hash=hash_print_agent_pairing_code("OTHR-PAIR"),
        exchange_token_hash=hash_print_agent_key("other-tenant-token"),
        status="PENDING",
        expires_at=datetime(2026, 3, 30, 13, 0, 0),
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    other_agent = PrintAgent(
        id=str(uuid.uuid4()),
        tenant_id=2,
        name="Other Tenant Agent",
        api_key=hash_print_agent_key("other-tenant-agent-key"),
        status="OFFLINE",
    )
    db_session.add(other_pairing)
    db_session.add(other_agent)
    db_session.commit()

    cancel_response = client.post(
        f"/admin/printing/agents/pairings/{other_pairing.id}/cancel",
        follow_redirects=True,
    )
    revoke_response = client.post(
        f"/admin/printing/agents/{other_agent.id}/revoke",
        follow_redirects=True,
    )

    assert cancel_response.status_code == 200
    assert "Print agent pairing was not found." in cancel_response.text
    assert revoke_response.status_code == 200
    assert "Print agent was not found." in revoke_response.text

    db_session.refresh(other_pairing)
    db_session.refresh(other_agent)
    assert other_pairing.status == "PENDING"
    assert other_agent.status == "OFFLINE"


def test_admin_destinations_enforces_single_default_per_document_type(client, db_session):
    template_a = _create_template(
        db_session,
        code="TICKET_DEFAULT_A",
        document_type="TICKET",
        template_format="TEXT",
        content="A {{ payload.ticket_no }}",
    )
    template_b = _create_template(
        db_session,
        code="TICKET_DEFAULT_B",
        document_type="TICKET",
        template_format="TEXT",
        content="B {{ payload.ticket_no }}",
    )

    first = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Destination A",
            "description": "A",
            "document_type": "TICKET",
            "template_id": str(template_a.id),
            "delivery_type": "PRINT_LOCAL_BROWSER",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    second = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Destination B",
            "description": "B",
            "document_type": "TICKET",
            "template_id": str(template_b.id),
            "delivery_type": "PRINT_LOCAL_BROWSER",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert second.status_code == 303

    defaults = db_session.execute(
        select(PrintDestination).where(
            PrintDestination.document_type == "TICKET",
            PrintDestination.is_active.is_(True),
            PrintDestination.is_default.is_(True),
        )
    ).scalars().all()

    assert len(defaults) == 1
    assert defaults[0].name == "Ticket Destination B"


def test_ticket_print_send_uses_default_destination_and_creates_job(
    client,
    db_session,
    monkeypatch,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-SEND-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_SEND_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="SEND {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="Ticket Network Printer",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NETWORK_RAW_9100",
        delivery_config={"host": "127.0.0.1", "port": 9100},
        is_default=True,
    )

    called: dict[str, object] = {}

    def _fake_send(job: bytes, mode: str, config: dict, **kwargs):
        called["job"] = job
        called["mode"] = mode
        called["config"] = dict(config)
        called["document_type"] = kwargs.get("document_type")
        return None

    monkeypatch.setattr(printing_service, "send_print_job", _fake_send)

    response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/tickets/{ticket.id}?")

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query.get("print_sent", [""])[0] == "1"

    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.ticket_id == ticket.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()

    assert job is not None
    assert job.status == "SENT"
    assert job.document_type == "TICKET"
    assert job.destination_id == destination.id

    assert called["mode"] == "network"
    assert called["document_type"] == "TICKET"
    assert isinstance(called["job"], bytes)


def test_ticket_preview_uses_default_destination_template(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-PREVIEW-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_PREVIEW_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PREVIEW_MARKER {{ payload.ticket_no }}",
    )
    _create_destination(
        db_session,
        name="Ticket Browser Preview",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=True,
    )

    response = client.get(f"/tickets/{ticket.id}/preview")

    assert response.status_code == 200
    assert "PREVIEW_MARKER T-PREVIEW-1" in response.text
    assert "DRAFT - PREVIEW ONLY" not in response.text


def test_ticket_preview_requires_complete_unless_dev_mode(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-DEV-PREVIEW-1",
        status=TicketStatusEnum.OPEN.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_DEV_PREVIEW_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="DEV {{ payload.ticket_no }}",
    )
    _create_destination(
        db_session,
        name="Ticket Dev Preview",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=True,
    )

    original_dev_mode = templates.env.globals.get("DEV_MODE", False)
    templates.env.globals["DEV_MODE"] = False
    try:
        blocked = client.get(f"/tickets/{ticket.id}/preview")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert blocked.status_code == 400

    templates.env.globals["DEV_MODE"] = True
    try:
        allowed = client.get(f"/tickets/{ticket.id}/preview")
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode

    assert allowed.status_code == 200
    assert "DRAFT - PREVIEW ONLY" in allowed.text


def test_ticket_print_missing_default_destination_shows_admin_config_error(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-NO-DEST-1",
        status=TicketStatusEnum.COMPLETE.value,
    )

    detail = client.get(f"/tickets/{ticket.id}")
    send = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert detail.status_code == 200
    assert "Printing is not configured. Contact admin." in detail.text
    assert send.status_code == 400
    assert "Printing is not configured. Contact admin." in send.text


def test_ticket_detail_documents_frame_groups_ticket_actions(client, db_session):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-UI-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_UI_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="UI {{ payload.ticket_no }}",
    )
    _create_destination(
        db_session,
        name="Ticket UI Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_LOCAL_BROWSER",
        delivery_config={},
        is_default=True,
    )

    response = client.get(f"/tickets/{ticket.id}")

    assert response.status_code == 200
    assert "Documents" in response.text
    assert "documents-panel" in response.text
    assert "documents-panel--header" in response.text
    assert "page-header__aside--documents" in response.text
    assert "ticket-header-actions" not in response.text
    assert "Preview" in response.text
    assert "Print" in response.text
    assert "Download PDF" in response.text
    assert f'href="/tickets/{ticket.id}/pdf"' in response.text
    assert response.text.index("page-header__aside--documents") < response.text.index("Ticket Info")
    assert "Preview Ticket" not in response.text
    assert "Print locally (browser)" not in response.text
    assert (
        "Browser printing may add URL/date/time headers/footers depending on your browser settings."
        not in response.text
    )
    assert "Advanced printing" not in response.text


def test_admin_node_http_destination_persists_printer_name_and_copies(client, db_session):
    template = _create_template(
        db_session,
        code="TICKET_NODE_HTTP_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="NODE {{ payload.ticket_no }}",
    )

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Site Agent",
            "description": "Node HTTP text destination",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_NODE_HTTP",
            "node_url": "http://127.0.0.1:9123/print",
            "node_api_key": "test-key",
            "node_timeout_ms": "7000",
            "node_printer_name": "Front Desk Printer",
            "node_copies": "2",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    destination = db_session.execute(
        select(PrintDestination)
        .where(PrintDestination.name == "Ticket Site Agent")
        .order_by(PrintDestination.id.desc())
    ).scalars().first()

    assert destination is not None
    assert destination.delivery_type == "PRINT_NODE_HTTP"
    assert destination.delivery_config["url"] == "http://127.0.0.1:9123/print"
    assert destination.delivery_config["api_key"] == "test-key"
    assert destination.delivery_config["timeout_ms"] == 7000
    assert destination.delivery_config["printer_name"] == "Front Desk Printer"
    assert destination.delivery_config["copies"] == 2

    edit_page = client.get(f"/admin/printing/destinations/{destination.id}/edit")

    assert edit_page.status_code == 200
    assert 'value="Front Desk Printer"' in edit_page.text
    assert 'name="node_copies"' in edit_page.text
    assert 'value="2"' in edit_page.text
    assert "Print: Site Agent HTTP" in edit_page.text


def test_admin_ticket_destination_persists_auto_print_on_complete_setting(
    client,
    db_session,
):
    template = _create_template(
        db_session,
        code="TICKET_AUTO_PRINT_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="AUTO {{ payload.ticket_no }}",
    )

    new_page = client.get("/admin/printing/destinations/new?document_type=TICKET")

    assert new_page.status_code == 200
    assert "Auto print when ticket is completed" in new_page.text

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Auto Print Destination",
            "description": "Auto print ticket destination",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_LOCAL_BROWSER",
            "auto_print_on_complete": "1",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    destination = db_session.execute(
        select(PrintDestination)
        .where(PrintDestination.name == "Ticket Auto Print Destination")
        .order_by(PrintDestination.id.desc())
    ).scalars().first()

    assert destination is not None
    assert destination.delivery_config["auto_print_on_complete"] is True

    edit_page = client.get(f"/admin/printing/destinations/{destination.id}/edit")

    assert edit_page.status_code == 200
    assert "Auto print when ticket is completed" in edit_page.text
    assert 'id="auto_print_on_complete"' in edit_page.text


def test_admin_agent_pull_destination_persists_assigned_agent_and_printer_settings(
    client,
    db_session,
):
    template = _create_template(
        db_session,
        code="TICKET_AGENT_PULL_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PULL {{ payload.ticket_no }}",
    )
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Yard Agent",
        api_key=hash_print_agent_key("agent-pull-key"),
        status="OFFLINE",
        printers_json=[
            {
                "name": "Polling Printer",
                "is_default": True,
                "is_online": True,
            },
            {
                "name": "Backup Printer",
                "is_default": False,
                "is_online": True,
            },
        ],
        printers_synced_at=datetime(2026, 3, 31, 9, 30, 0),
    )
    db_session.add(agent)
    db_session.commit()

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Agent Pull",
            "description": "Polling destination",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_AGENT_PULL",
            "pull_agent_id": agent.id,
            "pull_printer_name": "Polling Printer",
            "pull_copies": "2",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    destination = db_session.execute(
        select(PrintDestination)
        .where(PrintDestination.name == "Ticket Agent Pull")
        .order_by(PrintDestination.id.desc())
    ).scalars().first()

    assert destination is not None
    assert destination.delivery_type == "PRINT_AGENT_PULL"
    assert destination.delivery_config["agent_id"] == agent.id
    assert destination.delivery_config["printer_name"] == "Polling Printer"
    assert destination.delivery_config["copies"] == 2

    edit_page = client.get(f"/admin/printing/destinations/{destination.id}/edit")

    assert edit_page.status_code == 200
    assert "Polling Printer (default, online)" in edit_page.text
    assert "Backup Printer (online)" in edit_page.text
    assert "2 printers synced at 31/03/2026 09:30:00." in edit_page.text


def test_admin_agent_pull_destination_form_handles_no_synced_printers_cleanly(
    client,
    db_session,
):
    template = _create_template(
        db_session,
        code="TICKET_AGENT_PULL_NO_SYNC_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PULL {{ payload.ticket_no }}",
    )
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Yard Agent Without Sync",
        api_key=hash_print_agent_key("agent-pull-no-sync-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    destination = _create_destination(
        db_session,
        name="Ticket Agent Pull Manual",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config={
            "agent_id": agent.id,
            "printer_name": "Fallback Printer",
            "copies": 1,
        },
        is_default=False,
    )

    response = client.get(f"/admin/printing/destinations/{destination.id}/edit")

    assert response.status_code == 200
    assert "No printers synced from this agent yet" in response.text
    assert 'name="pull_printer_name_manual"' in response.text
    assert 'value="Fallback Printer"' in response.text


def test_admin_agent_pull_destination_manual_fallback_persists_when_no_synced_printers(
    client,
    db_session,
):
    template = _create_template(
        db_session,
        code="TICKET_AGENT_PULL_MANUAL_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PULL {{ payload.ticket_no }}",
    )
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Manual Fallback Agent",
        api_key=hash_print_agent_key("agent-pull-manual-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Agent Pull Manual Fallback",
            "description": "Polling destination without synced printers",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_AGENT_PULL",
            "pull_agent_id": agent.id,
            "pull_printer_name_manual": "Fallback Printer",
            "pull_copies": "2",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    destination = db_session.execute(
        select(PrintDestination)
        .where(PrintDestination.name == "Ticket Agent Pull Manual Fallback")
        .order_by(PrintDestination.id.desc())
    ).scalars().first()

    assert destination is not None
    assert destination.delivery_type == "PRINT_AGENT_PULL"
    assert destination.delivery_config["agent_id"] == agent.id
    assert destination.delivery_config["printer_name"] == "Fallback Printer"
    assert destination.delivery_config["copies"] == 2


def test_admin_agent_pull_destination_rejects_non_text_templates(client, db_session):
    template = _create_template(
        db_session,
        code="TICKET_AGENT_PULL_HTML_TEMPLATE",
        document_type="TICKET",
        template_format="HTML",
        content="<html><body>{{ payload.ticket_no }}</body></html>",
    )
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Yard Agent",
        api_key=hash_print_agent_key("agent-pull-html-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Agent Pull HTML",
            "description": "Invalid polling destination",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_AGENT_PULL",
            "pull_agent_id": agent.id,
            "pull_printer_name": "Polling Printer",
            "pull_copies": "1",
            "is_default": "1",
            "is_active": "1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 400
    assert "PRINT_AGENT_PULL destinations currently support TEXT templates only." in response.text


def test_admin_node_http_destination_rejects_non_text_templates(client, db_session):
    template = _create_template(
        db_session,
        code="TICKET_NODE_HTTP_HTML_TEMPLATE",
        document_type="TICKET",
        template_format="HTML",
        content="<html><body>{{ payload.ticket_no }}</body></html>",
    )

    response = client.post(
        "/admin/printing/destinations/new",
        data={
            "name": "Ticket Site Agent HTML",
            "description": "Invalid HTML destination",
            "document_type": "TICKET",
            "template_id": str(template.id),
            "delivery_type": "PRINT_NODE_HTTP",
            "node_url": "http://127.0.0.1:9123/print",
            "node_printer_name": "Front Desk Printer",
            "node_copies": "1",
            "is_default": "1",
            "is_active": "1",
        },
    )

    assert response.status_code == 400
    assert "PRINT_NODE_HTTP destinations currently support TEXT templates only." in response.text

    destination = db_session.execute(
        select(PrintDestination).where(PrintDestination.name == "Ticket Site Agent HTML")
    ).scalars().first()
    assert destination is None


def test_admin_print_job_detail_shows_requester_payload_and_provider_metadata(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-ADMIN-JOB-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_ADMIN_JOB_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="ADMIN {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="Admin Job Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
        is_default=True,
    )
    current_user = db_session.execute(
        select(User).order_by(User.id.asc()).limit(1)
    ).scalars().one()
    job = PrintJob(
        created_by_user_id=current_user.id,
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
        rendered_content="ADMIN T-ADMIN-JOB-1",
        rendered_bytes_base64=base64.b64encode(b"ADMIN T-ADMIN-JOB-1").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        provider_job_ref="agent-123",
        provider_response_json={
            "ok": True,
            "provider_job_ref": "agent-123",
            "message": "Accepted by agent",
        },
        status="SENT",
        attempt_count=1,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    response = client.get(f"/admin/printing/jobs/{job.id}")

    assert response.status_code == 200
    assert "Requested by" in response.text
    assert current_user.email in response.text
    assert "Payload format" in response.text
    assert "TEXT" in response.text
    assert "Payload MIME type" in response.text
    assert "text/plain; charset=utf-8" in response.text
    assert "Provider job ref" in response.text
    assert "agent-123" in response.text
    assert "Provider response JSON" in response.text
    assert "Accepted by agent" in response.text
    assert "Rendered Content" in response.text
    assert "ADMIN T-ADMIN-JOB-1" in response.text
    assert "Delivery Config Snapshot" in response.text
    assert "REDACTED" in response.text
    assert "live-secret" not in response.text
    assert "Trigger source" in response.text
    assert "Manual" in response.text
    assert "Front Desk Printer" in response.text
    assert f"/admin/printing/jobs/{job.id}/retry?return_to=detail" not in response.text


def test_admin_print_jobs_list_highlights_failed_jobs_and_shows_retry_action(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-ADMIN-JOBS-LIST-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_ADMIN_JOBS_LIST_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="LIST {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="List Retry Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "printer_name": "List Printer",
            "copies": 1,
        },
        is_default=True,
    )
    failed_job = PrintJob(
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "printer_name": "List Printer",
            "copies": 1,
        },
        rendered_content="LIST T-ADMIN-JOBS-LIST-1",
        rendered_bytes_base64=base64.b64encode(b"LIST T-ADMIN-JOBS-LIST-1").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        trigger_source="AUTO_ON_COMPLETE",
        status="FAILED",
        attempt_count=1,
        last_error="Printer offline",
    )
    sent_job = PrintJob(
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "printer_name": "List Printer",
            "copies": 1,
        },
        rendered_content="LIST T-ADMIN-JOBS-LIST-1 SENT",
        rendered_bytes_base64=base64.b64encode(b"LIST T-ADMIN-JOBS-LIST-1 SENT").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        trigger_source="MANUAL",
        status="SENT",
        attempt_count=1,
    )
    db_session.add_all([failed_job, sent_job])
    db_session.commit()
    db_session.refresh(failed_job)
    db_session.refresh(sent_job)

    response = client.get("/admin/printing/jobs")

    assert response.status_code == 200
    assert "Ticket T-ADMIN-JOBS-LIST-1" in response.text
    assert "Auto on complete" in response.text
    assert "List Printer" in response.text
    assert "Printer offline" in response.text
    assert f"/admin/printing/jobs/{failed_job.id}/retry?return_to=list" in response.text
    assert f"/admin/printing/jobs/{sent_job.id}/retry?return_to=list" not in response.text


def test_admin_print_job_detail_shows_failed_retry_context_and_retry_action(
    client,
    db_session,
):
    ticket = _create_ticket(
        db_session,
        ticket_no="T-ADMIN-JOB-FAIL-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_ADMIN_JOB_FAIL_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="FAIL {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="Admin Failed Job Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "printer_name": "Retry Printer",
            "copies": 1,
        },
        is_default=True,
    )
    job = PrintJob(
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "printer_name": "Retry Printer",
            "copies": 1,
        },
        rendered_content="FAIL T-ADMIN-JOB-FAIL-1",
        rendered_bytes_base64=base64.b64encode(b"FAIL T-ADMIN-JOB-FAIL-1").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        trigger_source="AUTO_ON_COMPLETE",
        status="FAILED",
        attempt_count=2,
        last_error="Agent offline",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    response = client.get(f"/admin/printing/jobs/{job.id}")

    assert response.status_code == 200
    assert "Auto on complete" in response.text
    assert "Retried before" in response.text
    assert "Yes" in response.text
    assert "Last error: Agent offline" in response.text
    assert f"/admin/printing/jobs/{job.id}/retry?return_to=detail" in response.text


def test_admin_retry_failed_print_agent_pull_job_uses_existing_retry_path(
    client,
    db_session,
):
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        name="Retry Pull Agent",
        api_key=hash_print_agent_key("retry-pull-agent-key"),
        status="OFFLINE",
    )
    db_session.add(agent)
    db_session.commit()

    ticket = _create_ticket(
        db_session,
        ticket_no="T-ADMIN-RETRY-PULL-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_ADMIN_RETRY_PULL_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="PULL {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="Admin Retry Pull Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config={
            "agent_id": agent.id,
            "printer_name": "Yard Pull Printer",
            "copies": 1,
        },
        is_default=True,
    )
    job = PrintJob(
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config_json={
            "agent_id": agent.id,
            "printer_name": "Yard Pull Printer",
            "copies": 1,
        },
        rendered_content="PULL T-ADMIN-RETRY-PULL-1",
        rendered_bytes_base64=base64.b64encode(b"PULL T-ADMIN-RETRY-PULL-1").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        status="FAILED",
        attempt_count=1,
        last_error="Printer jam",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    response = client.post(
        f"/admin/printing/jobs/{job.id}/retry?return_to=detail",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/printing/jobs/{job.id}?")
    assert "retry_success_message=" in response.headers["location"]
    db_session.refresh(job)
    assert job.status == "PENDING"
    assert job.attempt_count == 1
    assert job.last_error is None
    assert job.provider_job_ref is None
    assert job.provider_response_json is None


def test_admin_retry_failed_print_node_http_job_still_works(
    client,
    db_session,
    monkeypatch,
):
    import app.services.print_transport as transport_service

    ticket = _create_ticket(
        db_session,
        ticket_no="T-ADMIN-RETRY-NODE-1",
        status=TicketStatusEnum.COMPLETE.value,
    )
    template = _create_template(
        db_session,
        code="TICKET_ADMIN_RETRY_NODE_TEMPLATE",
        document_type="TICKET",
        template_format="TEXT",
        content="NODE {{ payload.ticket_no }}",
    )
    destination = _create_destination(
        db_session,
        name="Admin Retry Node Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "printer_name": "Retry Node Printer",
            "copies": 2,
        },
        is_default=True,
    )
    job = PrintJob(
        document_type="TICKET",
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "printer_name": "Retry Node Printer",
            "copies": 2,
        },
        rendered_content="NODE T-ADMIN-RETRY-NODE-1",
        rendered_bytes_base64=base64.b64encode(b"NODE T-ADMIN-RETRY-NODE-1").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type="text/plain; charset=utf-8",
        status="FAILED",
        attempt_count=1,
        last_error="Connection refused",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    called: dict[str, object] = {}

    def _fake_post(url, *, json, headers, timeout):
        called["url"] = url
        called["json"] = dict(json)
        called["headers"] = dict(headers)
        called["timeout"] = timeout
        return transport_service.httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "admin-retry-node-1",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    response = client.post(
        f"/admin/printing/jobs/{job.id}/retry?return_to=list",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/printing/jobs?")
    assert "retry_success_message=" in response.headers["location"]
    db_session.refresh(job)
    assert job.status == "SENT"
    assert job.attempt_count == 2
    assert job.provider_job_ref == "admin-retry-node-1"
    assert called["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "live-secret",
    }
    assert called["json"]["printer_name"] == "Retry Node Printer"
    assert called["json"]["copies"] == 2
