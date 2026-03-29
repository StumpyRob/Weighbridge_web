from datetime import datetime
import base64
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

import app.services.printing as printing_service
from app.models import (
    DirectionEnum,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    User,
)
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
