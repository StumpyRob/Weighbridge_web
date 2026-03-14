from __future__ import annotations

import app.services.email_service as email_service_module
from app.models import PlatformSetting


def test_send_email_with_attachment_uses_platform_settings_and_attachment(
    db_session,
    monkeypatch,
):
    db_session.add(
        PlatformSetting(
            smtp_host="smtp.mail.test",
            smtp_port=587,
            smtp_username="mailer@example.com",
            smtp_password="smtp-secret",
            smtp_from_email="platform@example.com",
            smtp_from_display_name="Weighbridge Platform",
            smtp_reply_to="reply@example.com",
            smtp_security="starttls",
        )
    )
    db_session.commit()

    sent: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host
            sent["port"] = port
            sent["timeout"] = timeout

        def ehlo(self):
            sent["ehlo"] = int(sent.get("ehlo", 0)) + 1

        def starttls(self):
            sent["started_tls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message, from_addr=None, to_addrs=None):
            sent["message"] = message
            sent["from_addr"] = from_addr
            sent["to_addrs"] = to_addrs

        def quit(self):
            sent["quit"] = True

    monkeypatch.setattr(email_service_module.smtplib, "SMTP", _FakeSMTP)

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
    assert sent["host"] == "smtp.mail.test"
    assert sent["port"] == 587
    assert sent["started_tls"] is True
    assert sent["login"] == ("mailer@example.com", "smtp-secret")
    assert sent["from_addr"] == "platform@example.com"
    assert sent["to_addrs"] == ["customer@example.com"]

    message = sent["message"]
    assert message["From"] == "Weighbridge Platform <platform@example.com>"
    assert message["Reply-To"] == "reply@example.com"

    html_parts = [
        part
        for part in message.walk()
        if part.get_content_type() == "text/html"
    ]
    assert html_parts
    assert "Attached invoice INV-1." in html_parts[0].get_content()

    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.get_filename() == "invoice-1.pdf"
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-1.4 test"
