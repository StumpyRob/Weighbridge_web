import base64

import httpx
import pytest
from sqlalchemy import select

import app.services.print_transport as transport_service
from app.models import PrintDestination, PrintJob, PrintTemplate
from app.services.printing import (
    DELIVERY_TYPE_PRINT_NODE_HTTP,
    DOCUMENT_TYPE_TICKET,
    PRINT_CONTENT_TYPE_HTML,
    PRINT_CONTENT_TYPE_TEXT,
    execute_rendered_print,
    replay_print_job,
    retry_print_job,
)
from app.services.print_transport import NODE_HTTP_TEXT_MIME_TYPE, send


def _create_node_http_destination(
    db_session,
    *,
    delivery_config: dict | None = None,
) -> PrintDestination:
    template = PrintTemplate(
        code="NODE_HTTP_TEXT_TEMPLATE",
        description="NODE_HTTP_TEXT_TEMPLATE",
        document_type="TICKET",
        format="TEXT",
        content="Ticket {{ payload.ticket_no }}",
        is_active=True,
    )
    db_session.add(template)
    db_session.flush()

    destination = PrintDestination(
        name="Node HTTP Destination",
        description="Node HTTP Destination",
        document_type="TICKET",
        template_id=template.id,
        delivery_type="PRINT_NODE_HTTP",
        delivery_config=delivery_config
        or {
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
        is_default=True,
        is_active=True,
    )
    db_session.add(destination)
    db_session.commit()
    db_session.refresh(destination)
    return destination


def test_local_node_http_sends_expected_json_payload_and_api_key(monkeypatch):
    called: dict[str, object] = {}

    def _fake_post(url, *, json, headers, timeout):
        called["url"] = url
        called["json"] = json
        called["headers"] = dict(headers)
        called["timeout"] = timeout
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "agent-123",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = send(
        b"HELLO\n",
        "local_node_http",
        {
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "site-secret",
            "timeout_ms": 7000,
            "printer_name": "Front Desk Printer",
            "copies": 2,
        },
        document_type="TICKET",
        job_id=123,
        job_name="TICKET print job 123",
        payload_format="TEXT",
        payload_mime_type=NODE_HTTP_TEXT_MIME_TYPE,
    )

    assert called["url"] == "http://127.0.0.1:9123/v1/print"
    assert called["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "site-secret",
    }
    assert called["timeout"] == 7.0
    assert called["json"] == {
        "job_id": "123",
        "document_type": "TICKET",
        "document_filename": "TICKET-123.txt",
        "job_name": "TICKET print job 123",
        "printer_name": "Front Desk Printer",
        "copies": 2,
        "payload_format": "TEXT",
        "payload_mime_type": NODE_HTTP_TEXT_MIME_TYPE,
        "payload_base64": base64.b64encode(b"HELLO\n").decode("ascii"),
    }
    assert result is not None
    assert result.provider_job_ref == "agent-123"
    assert result.provider_response_json == {
        "ok": True,
        "provider_job_ref": "agent-123",
        "message": "Accepted",
    }


def test_local_node_http_requires_http_2xx_and_ok_true(monkeypatch):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(503, json={"ok": False, "message": "Agent offline"})

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="Print node rejected job: Agent offline"):
        send(
            b"HELLO",
            "local_node_http",
            {
                "url": "http://127.0.0.1:9123/v1/print",
                "printer_name": "Front Desk Printer",
                "copies": 1,
            },
            document_type="TICKET",
            job_id=12,
            payload_format="TEXT",
        )


def test_local_node_http_rejects_ok_false(monkeypatch):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(
            200,
            json={"ok": False, "message": "Printer not found", "provider_job_ref": "agent-404"},
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="Print node rejected job: Printer not found"):
        send(
            b"HELLO",
            "local_node_http",
            {
                "url": "http://127.0.0.1:9123/v1/print",
                "printer_name": "Front Desk Printer",
                "copies": 1,
            },
            document_type="TICKET",
            job_id=99,
            payload_format="TEXT",
        )


def test_local_node_http_rejects_missing_ok(monkeypatch):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(200, json={"message": "Accepted maybe"})

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="Print node response missing ok flag."):
        send(
            b"HELLO",
            "local_node_http",
            {
                "url": "http://127.0.0.1:9123/v1/print",
                "printer_name": "Front Desk Printer",
                "copies": 1,
            },
            document_type="TICKET",
            job_id=77,
            payload_format="TEXT",
        )


def test_local_node_http_rejects_malformed_json_on_2xx(monkeypatch):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(200, text="not-json")

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="Print node returned invalid JSON response."):
        send(
            b"HELLO",
            "local_node_http",
            {
                "url": "http://127.0.0.1:9123/v1/print",
                "printer_name": "Front Desk Printer",
                "copies": 1,
            },
            document_type="TICKET",
            job_id=66,
            payload_format="TEXT",
        )


def test_execute_rendered_print_node_http_text_records_metadata_and_redacts_api_key(
    db_session,
    monkeypatch,
):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "agent-777",
                "message": "Accepted by agent",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = execute_rendered_print(
        db_session,
        document_type=DOCUMENT_TYPE_TICKET,
        rendered_content="Ticket T-1001\nCustomer: ACME",
        content_type=PRINT_CONTENT_TYPE_TEXT,
        delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "super-secret",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 2,
        },
    )

    job = result.job
    assert job.status == "SENT"
    assert job.payload_format == "TEXT"
    assert job.payload_mime_type == NODE_HTTP_TEXT_MIME_TYPE
    assert job.provider_job_ref == "agent-777"
    assert job.provider_response_json == {
        "ok": True,
        "provider_job_ref": "agent-777",
        "message": "Accepted by agent",
    }
    assert job.rendered_content == "Ticket T-1001\nCustomer: ACME"
    assert base64.b64decode(job.rendered_bytes_base64.encode("ascii")) == (
        b"Ticket T-1001\nCustomer: ACME"
    )
    assert job.delivery_config_json == {
        "url": "http://127.0.0.1:9123/v1/print",
        "api_key": "REDACTED",
        "timeout_ms": 5000,
        "printer_name": "Front Desk Printer",
        "copies": 2,
    }


def test_execute_rendered_print_node_http_persists_failure_response_json_when_possible(
    db_session,
    monkeypatch,
):
    def _fake_post(_url, *, json, headers, timeout):
        _ = (json, headers, timeout)
        return httpx.Response(
            200,
            json={
                "ok": False,
                "provider_job_ref": None,
                "message": "Printer not found",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="Print node rejected job: Printer not found"):
        execute_rendered_print(
            db_session,
            document_type=DOCUMENT_TYPE_TICKET,
            rendered_content="Ticket T-2002",
            content_type=PRINT_CONTENT_TYPE_TEXT,
            delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
            delivery_config={
                "url": "http://127.0.0.1:9123/v1/print",
                "api_key": "super-secret",
                "printer_name": "Missing Printer",
                "copies": 1,
            },
        )

    job = db_session.execute(
        select(PrintJob).order_by(PrintJob.id.desc())
    ).scalars().first()
    assert job is not None
    assert job.status == "FAILED"
    assert job.provider_job_ref is None
    assert job.provider_response_json == {
        "ok": False,
        "provider_job_ref": None,
        "message": "Printer not found",
    }
    assert job.last_error == "Print node rejected job: Printer not found"


def test_execute_rendered_print_node_http_html_is_converted_to_pdf(db_session, monkeypatch):
    called: dict[str, object] = {}

    def _fake_post(_url, *, json, headers, timeout):
        called["json"] = dict(json)
        called["headers"] = dict(headers)
        called["timeout"] = timeout
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "agent-html-1",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = execute_rendered_print(
        db_session,
        document_type=DOCUMENT_TYPE_TICKET,
        rendered_content="<html><body>Ticket T-3003</body></html>",
        content_type=PRINT_CONTENT_TYPE_HTML,
        delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
    )

    assert result.job.status == "SENT"
    assert result.job.payload_format == "PDF"
    assert result.job.payload_mime_type == "application/pdf"
    assert base64.b64decode(result.job.rendered_bytes_base64.encode("ascii")).startswith(b"%PDF")
    assert called["json"]["payload_format"] == "PDF"
    assert called["json"]["payload_mime_type"] == "application/pdf"
    assert called["json"]["document_filename"].endswith(".pdf")
    assert base64.b64decode(called["json"]["payload_base64"].encode("ascii")).startswith(b"%PDF")


def test_retry_print_job_node_http_uses_live_destination_api_key(
    db_session,
    monkeypatch,
):
    destination = _create_node_http_destination(
        db_session,
        delivery_config={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "live-secret",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 2,
        },
    )
    job = PrintJob(
        document_type=DOCUMENT_TYPE_TICKET,
        destination_id=destination.id,
        template_id=destination.template_id,
        delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 2,
        },
        rendered_content="Ticket T-4004\nCustomer: ACME",
        rendered_bytes_base64=base64.b64encode(b"Ticket T-4004\nCustomer: ACME").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type=NODE_HTTP_TEXT_MIME_TYPE,
        status="FAILED",
        attempt_count=1,
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
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "retry-agent-1",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = retry_print_job(db_session, job)

    assert result.job.status == "SENT"
    assert result.job.provider_job_ref == "retry-agent-1"
    assert result.job.provider_response_json == {
        "ok": True,
        "provider_job_ref": "retry-agent-1",
        "message": "Accepted",
    }
    assert called["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "live-secret",
    }
    assert called["json"] == {
        "job_id": str(job.id),
        "document_type": "TICKET",
        "document_filename": "Ticket-ticket-job.txt",
        "job_name": f"TICKET print job {job.id}",
        "printer_name": "Front Desk Printer",
        "copies": 2,
        "payload_format": "TEXT",
        "payload_mime_type": NODE_HTTP_TEXT_MIME_TYPE,
        "payload_base64": base64.b64encode(b"Ticket T-4004\nCustomer: ACME").decode("ascii"),
    }


def test_replay_print_job_node_http_uses_live_destination_api_key_and_new_user(
    db_session,
    monkeypatch,
):
    destination = _create_node_http_destination(db_session)
    original_job = PrintJob(
        created_by_user_id=11,
        document_type=DOCUMENT_TYPE_TICKET,
        destination_id=destination.id,
        template_id=destination.template_id,
        delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
        rendered_content="Ticket T-5005\nCustomer: ACME",
        rendered_bytes_base64=base64.b64encode(b"Ticket T-5005\nCustomer: ACME").decode("ascii"),
        payload_format="TEXT",
        payload_mime_type=NODE_HTTP_TEXT_MIME_TYPE,
        status="SENT",
        attempt_count=1,
    )
    db_session.add(original_job)
    db_session.commit()
    db_session.refresh(original_job)

    called: dict[str, object] = {}

    def _fake_post(url, *, json, headers, timeout):
        called["url"] = url
        called["json"] = dict(json)
        called["headers"] = dict(headers)
        called["timeout"] = timeout
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "replay-agent-1",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = replay_print_job(db_session, original_job, created_by_user_id=42)

    assert result.job.id != original_job.id
    assert result.job.created_by_user_id == 42
    assert result.job.status == "SENT"
    assert result.job.provider_job_ref == "replay-agent-1"
    assert result.job.provider_response_json == {
        "ok": True,
        "provider_job_ref": "replay-agent-1",
        "message": "Accepted",
    }
    assert result.job.delivery_config_json == {
        "url": "http://127.0.0.1:9123/v1/print",
        "api_key": "REDACTED",
        "timeout_ms": 5000,
        "printer_name": "Front Desk Printer",
        "copies": 1,
    }
    assert called["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "live-secret",
    }


def test_retry_print_job_node_http_legacy_text_job_without_payload_metadata_still_succeeds(
    db_session,
    monkeypatch,
):
    destination = _create_node_http_destination(db_session)
    job = PrintJob(
        document_type=DOCUMENT_TYPE_TICKET,
        destination_id=destination.id,
        template_id=destination.template_id,
        delivery_type=DELIVERY_TYPE_PRINT_NODE_HTTP,
        delivery_config_json={
            "url": "http://127.0.0.1:9123/v1/print",
            "api_key": "REDACTED",
            "timeout_ms": 5000,
            "printer_name": "Front Desk Printer",
            "copies": 1,
        },
        rendered_content="Legacy ticket text",
        rendered_bytes_base64=None,
        payload_format=None,
        payload_mime_type=None,
        status="FAILED",
        attempt_count=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    def _fake_post(_url, *, json, headers, timeout):
        _ = (headers, timeout)
        assert json["document_filename"] == "Ticket-ticket-job.txt"
        assert json["payload_format"] == "TEXT"
        assert json["payload_mime_type"] == NODE_HTTP_TEXT_MIME_TYPE
        assert json["payload_base64"] == base64.b64encode(b"Legacy ticket text").decode("ascii")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "provider_job_ref": "legacy-agent-1",
                "message": "Accepted",
            },
        )

    monkeypatch.setattr(transport_service.httpx, "post", _fake_post)

    result = retry_print_job(db_session, job)

    assert result.job.status == "SENT"
    assert result.job.payload_format == "TEXT"
    assert result.job.payload_mime_type == NODE_HTTP_TEXT_MIME_TYPE
    assert base64.b64decode(result.job.rendered_bytes_base64.encode("ascii")) == b"Legacy ticket text"
