import base64
import uuid
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import get_db
from app.main import app
from app.models import (
    DirectionEnum,
    PrintAgent,
    PrintAgentPairing,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Tenant,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
)
from app.security_hardening import CSRF_HEADER_NAME
from app.services.print_agents import (
    PRINT_AGENT_STATUS_OFFLINE,
    hash_print_agent_key,
)
from app.services.printing import (
    DELIVERY_TYPE_PRINT_AGENT_PULL,
    DOCUMENT_TYPE_TICKET,
    PRINT_CONTENT_TYPE_PDF,
    PRINT_CONTENT_TYPE_TEXT,
    execute_rendered_print,
)


def _client_for_base_url(SessionLocal, *, base_url: str) -> TestClient:
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, base_url=base_url)


def _create_print_agent(
    db_session,
    *,
    raw_api_key: str,
    name: str | None = None,
    tenant_id: int = 1,
    printers_json: list[dict[str, object]] | None = None,
    printers_synced_at: datetime | None = None,
) -> PrintAgent:
    agent = PrintAgent(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=name,
        api_key=hash_print_agent_key(raw_api_key),
        status=PRINT_AGENT_STATUS_OFFLINE,
        printers_json=printers_json,
        printers_synced_at=printers_synced_at,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


def _create_pull_template_and_destination(
    db_session,
    *,
    agent_id: str,
    printer_name: str = "Yard Printer",
    copies: int = 1,
    template_code: str = "PULL_TEMPLATE",
    is_default: bool = False,
) -> tuple[PrintTemplate, PrintDestination]:
    template = PrintTemplate(
        code=template_code,
        description=template_code,
        document_type="TICKET",
        format="TEXT",
        content="PULL {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name=f"{template_code} Destination",
        description=f"{template_code} Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_AGENT_PULL",
        delivery_config={
            "agent_id": agent_id,
            "printer_name": printer_name,
            "copies": copies,
        },
        is_default=is_default,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(template)
    db_session.refresh(destination)
    return template, destination


def _create_complete_ticket(db_session, *, ticket_no: str) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 3, 29, 12, 0, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.SALE.value,
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _create_tenant(
    db_session,
    *,
    name: str,
    subdomain: str,
    is_active: bool = True,
) -> Tenant:
    tenant = Tenant(
        name=name,
        subdomain=subdomain,
        is_active=is_active,
    )
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


def _create_pull_job(
    db_session,
    *,
    agent_id: str,
    printer_name: str = "Yard Printer",
    copies: int = 1,
    rendered_content: str = "Ticket T-PULL-1\nCustomer: ACME",
) -> PrintJob:
    template, destination = _create_pull_template_and_destination(
        db_session,
        agent_id=agent_id,
        printer_name=printer_name,
        copies=copies,
        template_code=f"PULL_TEMPLATE_{uuid.uuid4().hex[:8]}",
    )
    ticket = _create_complete_ticket(
        db_session,
        ticket_no=f"T-PULL-{uuid.uuid4().hex[:6].upper()}",
    )
    result = execute_rendered_print(
        db_session,
        document_type=DOCUMENT_TYPE_TICKET,
        rendered_content=rendered_content,
        content_type=PRINT_CONTENT_TYPE_TEXT,
        delivery_type=DELIVERY_TYPE_PRINT_AGENT_PULL,
        delivery_config={
            "agent_id": agent_id,
            "printer_name": printer_name,
            "copies": copies,
        },
        destination_id=destination.id,
        template_id=template.id,
        ticket_id=ticket.id,
    )
    return result.job


def test_print_agent_register_returns_id_and_api_key(client, db_session):
    response = client.post(
        "/api/print/agents/register",
        json={"name": "Yard Printer Agent"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"]
    assert payload["api_key"]

    agent = db_session.get(PrintAgent, payload["agent_id"])
    assert agent is not None
    assert agent.name == "Yard Printer Agent"
    assert agent.api_key == hash_print_agent_key(payload["api_key"])
    assert agent.api_key != payload["api_key"]


def test_print_agent_pairing_happy_path_returns_agent_credentials(
    client,
    client_anonymous,
    db_session,
):
    request_response = client_anonymous.post(
        "/api/print/agents/pairing/request",
        json={"name": "Local Yard Agent"},
    )

    assert request_response.status_code == 200
    request_payload = request_response.json()
    assert request_payload["pairing_id"]
    assert request_payload["pairing_code"]
    assert request_payload["exchange_token"]
    assert request_payload["status"] == "PENDING"

    complete_response = client.post(
        "/api/print/agents/pairing/complete",
        json={
            "pairing_code": request_payload["pairing_code"],
            "name": "Paired Yard Agent",
        },
    )

    assert complete_response.status_code == 200
    complete_payload = complete_response.json()
    assert complete_payload["ok"] is True
    assert complete_payload["status"] == "PAIRED"
    paired_agent = db_session.get(PrintAgent, complete_payload["agent_id"])
    assert paired_agent is not None
    assert paired_agent.name == "Paired Yard Agent"
    placeholder_api_key_hash = paired_agent.api_key

    pairing = db_session.get(PrintAgentPairing, request_payload["pairing_id"])
    assert pairing is not None
    assert pairing.status == "PAIRED"
    assert pairing.print_agent_id == paired_agent.id
    assert pairing.paired_by_user_id is not None

    exchange_response = client_anonymous.post(
        "/api/print/agents/pairing/exchange",
        json={
            "pairing_id": request_payload["pairing_id"],
            "exchange_token": request_payload["exchange_token"],
        },
    )

    assert exchange_response.status_code == 200
    exchange_payload = exchange_response.json()
    assert exchange_payload["agent_id"] == paired_agent.id
    assert exchange_payload["api_key"]

    db_session.refresh(paired_agent)
    db_session.refresh(pairing)
    assert paired_agent.api_key == hash_print_agent_key(exchange_payload["api_key"])
    assert paired_agent.api_key != placeholder_api_key_hash
    assert pairing.status == "EXCHANGED"
    assert pairing.exchanged_at is not None


def test_print_agent_pairing_rejects_invalid_code(client):
    response = client.post(
        "/api/print/agents/pairing/complete",
        json={"pairing_code": "BAD-CODE"},
    )

    assert response.status_code == 404


def test_print_agent_pairing_rejects_expired_code(client, client_anonymous, db_session):
    request_response = client_anonymous.post(
        "/api/print/agents/pairing/request",
        json={"name": "Expiring Agent"},
    )
    assert request_response.status_code == 200
    request_payload = request_response.json()

    pairing = db_session.get(PrintAgentPairing, request_payload["pairing_id"])
    assert pairing is not None
    pairing.expires_at = datetime(2026, 3, 29, 11, 0, 0)
    db_session.commit()

    response = client.post(
        "/api/print/agents/pairing/complete",
        json={"pairing_code": request_payload["pairing_code"]},
    )

    assert response.status_code == 410


def test_print_agent_pairing_code_is_one_time_use(client, client_anonymous):
    request_response = client_anonymous.post(
        "/api/print/agents/pairing/request",
        json={"name": "One Time Agent"},
    )
    assert request_response.status_code == 200
    request_payload = request_response.json()

    first = client.post(
        "/api/print/agents/pairing/complete",
        json={"pairing_code": request_payload["pairing_code"]},
    )
    second = client.post(
        "/api/print/agents/pairing/complete",
        json={"pairing_code": request_payload["pairing_code"]},
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_print_agent_heartbeat_updates_last_seen(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-heartbeat-key",
        name="Heartbeat Agent",
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    response = client_anonymous.post(
        "/api/print/agents/heartbeat",
        headers={"X-Agent-Key": "agent-heartbeat-key"},
    )

    assert response.status_code == 200
    db_session.refresh(agent)
    assert agent.status == "ONLINE"
    assert agent.last_seen_at is not None


def test_print_agent_printer_sync_stores_snapshot_and_synced_at(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-sync-key",
        name="Sync Agent",
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    response = client_anonymous.post(
        "/api/print/agents/printers/sync",
        headers={"X-Agent-Key": "agent-sync-key"},
        json={
            "printers": [
                {
                    "name": "  Zebra ZSB-DP14  ",
                    "is_default": False,
                    "is_online": True,
                },
                {
                    "name": "Microsoft Print to PDF",
                    "is_default": True,
                    "is_online": True,
                },
                {
                    "name": "zebra zsb-dp14",
                    "is_default": True,
                    "is_online": False,
                },
                {
                    "name": "   ",
                    "is_default": False,
                    "is_online": True,
                },
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["printer_count"] == 2

    db_session.refresh(agent)
    assert agent.status == "ONLINE"
    assert agent.printers_synced_at is not None
    assert agent.printers_json == [
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
    ]


def test_print_agent_printer_sync_replaces_removed_printers(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-sync-replace-key",
        name="Sync Replace Agent",
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    first = client_anonymous.post(
        "/api/print/agents/printers/sync",
        headers={"X-Agent-Key": "agent-sync-replace-key"},
        json={
            "printers": [
                {"name": "Front Desk Printer", "is_default": True, "is_online": True},
                {"name": "Warehouse Printer", "is_default": False, "is_online": True},
            ]
        },
    )

    assert first.status_code == 200

    second = client_anonymous.post(
        "/api/print/agents/printers/sync",
        headers={"X-Agent-Key": "agent-sync-replace-key"},
        json={
            "printers": [
                {"name": "Warehouse Printer", "is_default": True, "is_online": False},
            ]
        },
    )

    assert second.status_code == 200
    assert second.json()["printer_count"] == 1

    db_session.refresh(agent)
    assert agent.printers_json == [
        {
            "name": "Warehouse Printer",
            "is_default": True,
            "is_online": False,
        }
    ]


def test_print_agent_printer_sync_requires_agent_key(client_anonymous):
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    response = client_anonymous.post(
        "/api/print/agents/printers/sync",
        json={"printers": []},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "X-Agent-Key header is required."


def test_print_agent_printer_sync_is_tenant_scoped(client_anonymous, db_session):
    other_tenant_agent = _create_print_agent(
        db_session,
        raw_api_key="other-tenant-sync-key",
        name="Other Tenant Agent",
        tenant_id=2,
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    response = client_anonymous.post(
        "/api/print/agents/printers/sync",
        headers={"X-Agent-Key": "other-tenant-sync-key"},
        json={"printers": [{"name": "Other Tenant Printer", "is_online": True}]},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid agent key."

    db_session.refresh(other_tenant_agent)
    assert other_tenant_agent.printers_json is None
    assert other_tenant_agent.printers_synced_at is None


def test_print_agent_poll_returns_correct_job(client_anonymous, db_session):
    assigned_agent = _create_print_agent(
        db_session,
        raw_api_key="assigned-agent-key",
        name="Assigned Agent",
    )
    other_agent = _create_print_agent(
        db_session,
        raw_api_key="other-agent-key",
        name="Other Agent",
    )
    job = _create_pull_job(
        db_session,
        agent_id=assigned_agent.id,
        printer_name="Front Desk Printer",
        copies=2,
        rendered_content="Ticket T-POLL-1\nCustomer: ACME",
    )
    _create_pull_job(
        db_session,
        agent_id=other_agent.id,
        rendered_content="Ticket T-POLL-2\nCustomer: BRAVO",
    )

    response = client_anonymous.get(
        "/api/print/agents/jobs/next",
        headers={"X-Agent-Key": "assigned-agent-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["job_id"] == job.id
    assert payload["job"]["document_type"] == "TICKET"
    assert payload["job"]["job_name"] == f"TICKET print job {job.id}"
    assert payload["job"]["document_filename"].endswith(".pdf")
    assert payload["job"]["payload_format"] == "PDF"
    assert payload["job"]["payload_mime_type"] == "application/pdf"
    assert payload["job"]["payload_url"].endswith(f"/api/print/jobs/{job.id}/payload")
    assert "payload_base64" not in payload["job"]
    assert payload["job"]["printer_name"] == "Front Desk Printer"
    assert payload["job"]["copies"] == 2


def test_print_agent_poll_payload_url_uses_https_tenant_origin_when_request_scheme_is_http(
    SessionLocal,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    assigned_agent = _create_print_agent(
        db_session,
        raw_api_key="assigned-http-origin-key",
        name="Assigned Agent",
    )
    job = _create_pull_job(
        db_session,
        agent_id=assigned_agent.id,
        rendered_content="Ticket T-POLL-HTTP\nCustomer: ACME",
    )

    try:
        with _client_for_base_url(SessionLocal, base_url="http://demo.example.test") as client:
            response = client.get(
                "/api/print/agents/jobs/next",
                headers={"X-Agent-Key": "assigned-http-origin-key"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["payload_url"] == (
        f"https://demo.example.test/api/print/jobs/{job.id}/payload"
    )


def test_print_agent_poll_payload_url_preserves_path_based_tenant_routes(
    SessionLocal,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "localhost")
    tenant = _create_tenant(
        db_session,
        name="Demo Tenant",
        subdomain="demo",
    )
    assigned_agent = _create_print_agent(
        db_session,
        raw_api_key="assigned-path-based-key",
        name="Assigned Agent",
        tenant_id=tenant.id,
    )
    job = _create_pull_job(
        db_session,
        agent_id=assigned_agent.id,
        rendered_content="Ticket T-POLL-PATH\nCustomer: ACME",
    )

    try:
        with _client_for_base_url(SessionLocal, base_url="https://localhost:8443") as client:
            response = client.get(
                "/t/demo/api/print/agents/jobs/next",
                headers={"X-Agent-Key": "assigned-path-based-key"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["payload_url"] == (
        f"https://localhost:8443/t/demo/api/print/jobs/{job.id}/payload"
    )


def test_print_agent_poll_payload_url_preserves_localhost_http(SessionLocal, db_session, monkeypatch):
    monkeypatch.setattr(settings, "base_domain", "")
    assigned_agent = _create_print_agent(
        db_session,
        raw_api_key="assigned-local-http-key",
        name="Assigned Agent",
    )
    job = _create_pull_job(
        db_session,
        agent_id=assigned_agent.id,
        rendered_content="Ticket T-POLL-LOCAL\nCustomer: ACME",
    )

    try:
        with _client_for_base_url(SessionLocal, base_url="http://localhost:8080") as client:
            response = client.get(
                "/api/print/agents/jobs/next",
                headers={"X-Agent-Key": "assigned-local-http-key"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["job"]["payload_url"] == (
        f"http://localhost:8080/api/print/jobs/{job.id}/payload"
    )


def test_print_agent_claim_updates_job_and_prevents_double_claim(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-claim-key",
        name="Claim Agent",
    )
    job = _create_pull_job(
        db_session,
        agent_id=agent.id,
        printer_name="Claim Printer",
        copies=3,
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)

    first = client_anonymous.post(
        f"/api/print/jobs/{job.id}/claim",
        headers={"X-Agent-Key": "agent-claim-key"},
    )
    second = client_anonymous.post(
        f"/api/print/jobs/{job.id}/claim",
        headers={"X-Agent-Key": "agent-claim-key"},
    )

    assert first.status_code == 200
    assert first.json()["printer_name"] == "Claim Printer"
    assert first.json()["job"]["payload_format"] == "PDF"
    assert first.json()["job"]["payload_url"].endswith(f"/api/print/jobs/{job.id}/payload")
    assert first.json()["copies"] == 3
    assert second.status_code == 409
    db_session.refresh(job)
    assert job.status == "IN_PROGRESS"
    assert job.agent_id == agent.id
    assert job.attempt_count == 1


def test_print_agent_payload_returns_claimed_job_payload(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-payload-key",
        name="Payload Agent",
    )
    job = _create_pull_job(
        db_session,
        agent_id=agent.id,
        printer_name="Payload Printer",
        copies=4,
        rendered_content="Ticket T-PAYLOAD-1\nCustomer: ACME",
    )
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)
    client_anonymous.post(
        f"/api/print/jobs/{job.id}/claim",
        headers={"X-Agent-Key": "agent-payload-key"},
    )

    response = client_anonymous.get(
        f"/api/print/jobs/{job.id}/payload",
        headers={"X-Agent-Key": "agent-payload-key"},
    )

    assert response.status_code == 200
    assert response.headers["x-print-job-id"] == str(job.id)
    assert response.headers["x-print-payload-format"] == "PDF"
    assert response.headers["x-print-payload-mime-type"] == "application/pdf"
    assert response.headers["x-print-printer-name"] == "Payload Printer"
    assert response.headers["x-print-copies"] == "4"
    assert response.headers["content-disposition"].endswith('.pdf"')
    assert response.content.startswith(b"%PDF")


def test_print_agent_complete_marks_job_sent(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-complete-key",
        name="Complete Agent",
    )
    job = _create_pull_job(db_session, agent_id=agent.id)
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)
    client_anonymous.post(
        f"/api/print/jobs/{job.id}/claim",
        headers={"X-Agent-Key": "agent-complete-key"},
    )

    response = client_anonymous.post(
        f"/api/print/jobs/{job.id}/complete",
        headers={"X-Agent-Key": "agent-complete-key"},
        json={
            "provider_job_ref": "agent-job-123",
            "provider_response_json": {
                "ok": True,
                "provider_job_ref": "agent-job-123",
                "message": "Printed",
            },
        },
    )

    assert response.status_code == 200
    db_session.refresh(job)
    assert job.status == "SENT"
    assert job.provider_job_ref == "agent-job-123"
    assert job.provider_response_json == {
        "ok": True,
        "provider_job_ref": "agent-job-123",
        "message": "Printed",
    }
    assert job.sent_at is not None


def test_print_agent_fail_marks_job_failed(client_anonymous, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-fail-key",
        name="Fail Agent",
    )
    job = _create_pull_job(db_session, agent_id=agent.id)
    client_anonymous.headers.pop(CSRF_HEADER_NAME, None)
    client_anonymous.post(
        f"/api/print/jobs/{job.id}/claim",
        headers={"X-Agent-Key": "agent-fail-key"},
    )

    response = client_anonymous.post(
        f"/api/print/jobs/{job.id}/fail",
        headers={"X-Agent-Key": "agent-fail-key"},
        json={
            "error": "Printer out of paper",
            "provider_response_json": {
                "ok": False,
                "message": "Printer out of paper",
            },
        },
    )

    assert response.status_code == 200
    db_session.refresh(job)
    assert job.status == "FAILED"
    assert job.last_error == "Printer out of paper"
    assert job.provider_response_json == {
        "ok": False,
        "message": "Printer out of paper",
    }


def test_ticket_print_pull_destination_creates_pending_job(client, db_session):
    agent = _create_print_agent(
        db_session,
        raw_api_key="agent-route-key",
        name="Route Agent",
    )
    ticket = _create_complete_ticket(db_session, ticket_no="T-PULL-ROUTE-1")
    template, destination = _create_pull_template_and_destination(
        db_session,
        agent_id=agent.id,
        template_code="PULL_ROUTE_TEMPLATE",
        is_default=True,
    )
    _ = template

    response = client.post(f"/tickets/{ticket.id}/print", follow_redirects=False)

    assert response.status_code == 303
    job = db_session.execute(
        select(PrintJob)
        .where(PrintJob.ticket_id == ticket.id)
        .order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is not None
    assert job.destination_id == destination.id
    assert job.delivery_type == DELIVERY_TYPE_PRINT_AGENT_PULL
    assert job.status == "PENDING"
    assert job.payload_format == PRINT_CONTENT_TYPE_PDF
    assert job.payload_mime_type == "application/pdf"
    assert base64.b64decode(job.rendered_bytes_base64.encode("ascii")).startswith(b"%PDF")
