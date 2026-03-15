from __future__ import annotations

import base64

import app.services.email_service as email_service_module
from app.models import PlatformSetting


def test_send_email_with_attachment_uses_resend_settings_and_attachment(
    db_session,
    monkeypatch,
):
    db_session.add(
        PlatformSetting(
            email_provider="resend",
            resend_api_key="re_test_api_key",
            from_email="platform@example.com",
            from_display_name="Weighbridge Platform",
            reply_to="reply@example.com",
        )
    )
    db_session.commit()

    sent: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        text = '{"id":"email_123"}'

        def raise_for_status(self) -> None:
            return None

    def _fake_post(url, *, headers, json, timeout):
        sent["url"] = url
        sent["headers"] = headers
        sent["json"] = json
        sent["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(email_service_module.httpx, "post", _fake_post)

    result = email_service_module.send_email_with_attachment(
        subject="Invoice INV-1",
        text_body="Attached invoice INV-1.",
        html_body="<p>Attached invoice INV-1.</p>",
        to=["customer@example.com"],
        attachment=email_service_module.EmailAttachment(
            filename="invoice-1.pdf",
            content_bytes=b"%PDF-1.4 test",
            content_type="application/pdf",
        ),
        db=db_session,
    )

    assert result.ok is True
    assert sent["url"] == email_service_module.RESEND_SEND_EMAIL_URL
    assert sent["headers"] == {"Authorization": "Bearer re_test_api_key"}
    assert sent["timeout"] == email_service_module.DEFAULT_RESEND_TIMEOUT_SECONDS

    payload = sent["json"]
    assert payload["from"] == "Weighbridge Platform <platform@example.com>"
    assert payload["reply_to"] == "reply@example.com"
    assert payload["to"] == ["customer@example.com"]
    assert payload["subject"] == "Invoice INV-1"
    assert payload["text"] == "Attached invoice INV-1."
    assert payload["html"] == "<p>Attached invoice INV-1.</p>"

    attachments = payload["attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["filename"] == "invoice-1.pdf"
    assert attachment["content_type"] == "application/pdf"
    assert base64.b64decode(attachment["content"]) == b"%PDF-1.4 test"
