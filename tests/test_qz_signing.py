from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from cryptography.x509.oid import NameOID

from app.config import settings
from app.models import PlatformSetting
from app.services import qz_signing
from app.services.platform_qz_credentials import save_platform_qz_credentials


def _write_qz_test_keys(tmp_path: Path) -> tuple[Path, Path, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "GB"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Weighbridge Web Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "QZ Signing Test"),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )

    certificate_path = tmp_path / "digital-certificate.txt"
    private_key_path = tmp_path / "private-key.pem"
    certificate_text = certificate.public_bytes(Encoding.PEM).decode("utf-8")
    certificate_path.write_text(certificate_text, encoding="utf-8")
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
    )
    return certificate_path, private_key_path, certificate_text


def _clear_qz_signing_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "qz_certificate_text", "")
    monkeypatch.setattr(settings, "qz_private_key_text", "")
    monkeypatch.setattr(settings, "qz_certificate_path", "")
    monkeypatch.setattr(settings, "qz_private_key_path", "")
    for env_name in (
        "QZ_CERTIFICATE_TEXT",
        "QZ_PRIVATE_KEY_TEXT",
        "QZ_CERTIFICATE_PATH",
        "QZ_PRIVATE_KEY_PATH",
        "QZ_CERTIFICATE_FILE",
        "QZ_PRIVATE_KEY_FILE",
        "RAILWAY_VOLUME_MOUNT_PATH",
    ):
        monkeypatch.delenv(env_name, raising=False)


def test_qz_certificate_route_returns_configured_certificate(
    client_anonymous,
    monkeypatch,
    tmp_path,
):
    certificate_path, private_key_path, certificate_text = _write_qz_test_keys(tmp_path)
    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(settings, "qz_certificate_path", str(certificate_path))
    monkeypatch.setattr(settings, "qz_private_key_path", str(private_key_path))

    response = client_anonymous.get("/qz/certificate")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers.get("cache-control") == "no-store"
    assert response.text == certificate_text.strip()


def test_qz_sign_route_returns_sha512_signature_for_request(
    client_anonymous,
    monkeypatch,
    tmp_path,
):
    certificate_path, private_key_path, certificate_text = _write_qz_test_keys(tmp_path)
    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(settings, "qz_certificate_path", str(certificate_path))
    monkeypatch.setattr(settings, "qz_private_key_path", str(private_key_path))

    to_sign = '{"call":"printers.getDefault","params":null,"timestamp":1234567890}'
    response = client_anonymous.post("/qz/sign", json={"request": to_sign})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers.get("cache-control") == "no-store"

    signature = base64.b64decode(response.text.encode("ascii"))
    certificate = x509.load_pem_x509_certificate(certificate_text.encode("utf-8"))
    certificate.public_key().verify(
        signature,
        to_sign.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )


def test_qz_signing_routes_work_with_inline_env_config_when_dev_mode_is_false(
    client_anonymous,
    monkeypatch,
    tmp_path,
):
    _certificate_path, private_key_path, certificate_text = _write_qz_test_keys(tmp_path)

    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setenv("QZ_CERTIFICATE_TEXT", certificate_text.replace("\n", "\\n"))
    monkeypatch.setenv("QZ_PRIVATE_KEY_FILE", str(private_key_path))

    certificate_response = client_anonymous.get("/qz/certificate")
    sign_response = client_anonymous.post("/qz/sign", json={"request": "signed-request"})

    assert certificate_response.status_code == 200
    assert certificate_response.text.strip() == certificate_text.strip()
    assert sign_response.status_code == 200
    assert sign_response.text.strip()


def test_qz_certificate_route_uses_railway_volume_qz_mount_when_present(
    client_anonymous,
    monkeypatch,
    tmp_path,
):
    mount_root = tmp_path / "railway-volume"
    qz_dir = mount_root / "qz"
    qz_dir.mkdir(parents=True, exist_ok=True)
    _certificate_path, _private_key_path, certificate_text = _write_qz_test_keys(qz_dir)

    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(mount_root))

    response = client_anonymous.get("/qz/certificate")

    assert response.status_code == 200
    assert response.text == certificate_text.strip()


def test_qz_signing_does_not_use_desktop_demo_fallback_when_dev_mode_is_false(
    client_anonymous,
    monkeypatch,
    tmp_path,
):
    certificate_dir = tmp_path / "Desktop" / "QZ Tray Demo Cert"
    certificate_dir.mkdir(parents=True, exist_ok=True)
    _write_qz_test_keys(certificate_dir)

    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(qz_signing.Path, "home", lambda: tmp_path)

    certificate_response = client_anonymous.get("/qz/certificate")
    sign_response = client_anonymous.post("/qz/sign", json={"request": "demo-request"})

    assert certificate_response.status_code == 503
    assert certificate_response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE
    assert sign_response.status_code == 503
    assert sign_response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE


def test_qz_certificate_route_returns_503_with_admin_config_error_when_missing(
    client_anonymous,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)

    response = client_anonymous.get("/qz/certificate")

    assert response.status_code == 503
    assert response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE
    assert "QZ_CERTIFICATE_TEXT" not in response.text
    assert "QZ_CERTIFICATE_PATH" not in response.text
    assert "QZ_CERTIFICATE_FILE" not in response.text
    assert "$RAILWAY_VOLUME_MOUNT_PATH/qz/digital-certificate.txt" not in response.text


def test_qz_routes_return_disabled_message_when_platform_setting_turns_off_qz(
    client_anonymous,
    db_session,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)
    db_session.add(PlatformSetting(qz_enabled=False))
    db_session.commit()

    certificate_response = client_anonymous.get("/qz/certificate")
    sign_response = client_anonymous.post("/qz/sign", json={"request": "demo-request"})

    assert certificate_response.status_code == 503
    assert certificate_response.text == qz_signing.QZ_PUBLIC_DISABLED_MESSAGE
    assert sign_response.status_code == 503
    assert sign_response.text == qz_signing.QZ_PUBLIC_DISABLED_MESSAGE


def test_qz_sign_route_returns_503_with_admin_config_error_when_missing(
    client_anonymous,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)

    response = client_anonymous.post("/qz/sign", json={"request": "demo-request"})

    assert response.status_code == 503
    assert response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE
    assert "QZ_PRIVATE_KEY_TEXT" not in response.text
    assert "QZ_PRIVATE_KEY_PATH" not in response.text
    assert "QZ_PRIVATE_KEY_FILE" not in response.text
    assert "$RAILWAY_VOLUME_MOUNT_PATH/qz/private-key.pem" not in response.text


def test_qz_signing_diagnostics_report_missing_configuration(monkeypatch):
    _clear_qz_signing_config(monkeypatch)

    diagnostics = qz_signing.build_qz_signing_diagnostics(enabled=True)

    assert diagnostics.enabled is True
    assert diagnostics.certificate.configured is False
    assert diagnostics.private_key.configured is False
    assert diagnostics.certificate_route.ok is False
    assert diagnostics.sign_route.ok is False
    assert diagnostics.signing_operational is False
    assert diagnostics.ready_for_tenants is False
    assert diagnostics.likely_causes


def test_qz_signing_diagnostics_report_ready_when_configured(monkeypatch, tmp_path):
    certificate_path, private_key_path, _certificate_text = _write_qz_test_keys(tmp_path)
    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(settings, "qz_certificate_path", str(certificate_path))
    monkeypatch.setattr(settings, "qz_private_key_path", str(private_key_path))

    diagnostics = qz_signing.build_qz_signing_diagnostics(enabled=True)

    assert diagnostics.certificate.configured is True
    assert diagnostics.private_key.configured is True
    assert diagnostics.certificate_route.ok is True
    assert diagnostics.sign_route.ok is True
    assert diagnostics.signing_operational is True
    assert diagnostics.ready_for_tenants is True


def test_qz_routes_prefer_saved_platform_credentials_over_legacy_fallback(
    client_anonymous,
    db_session,
    monkeypatch,
    tmp_path,
):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_certificate_path, legacy_private_key_path, legacy_certificate_text = _write_qz_test_keys(
        legacy_dir
    )
    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(settings, "qz_certificate_path", str(legacy_certificate_path))
    monkeypatch.setattr(settings, "qz_private_key_path", str(legacy_private_key_path))

    stored_dir = tmp_path / "stored"
    stored_dir.mkdir(parents=True, exist_ok=True)
    stored_certificate_path, stored_private_key_path, stored_certificate_text = _write_qz_test_keys(
        stored_dir
    )
    stored_private_key_text = stored_private_key_path.read_text(encoding="utf-8")
    save_platform_qz_credentials(
        db_session,
        certificate_text=stored_certificate_text,
        private_key_text=stored_private_key_text,
    )
    db_session.commit()

    certificate_response = client_anonymous.get("/qz/certificate")
    sign_response = client_anonymous.post("/qz/sign", json={"request": "stored-request"})

    assert certificate_response.status_code == 200
    assert certificate_response.text.strip() == stored_certificate_text.strip()
    assert certificate_response.text.strip() != legacy_certificate_text.strip()
    assert sign_response.status_code == 200

    signature = base64.b64decode(sign_response.text.encode("ascii"))
    certificate = x509.load_pem_x509_certificate(stored_certificate_text.encode("utf-8"))
    certificate.public_key().verify(
        signature,
        b"stored-request",
        padding.PKCS1v15(),
        hashes.SHA512(),
    )


def test_qz_routes_keep_public_errors_generic_when_saved_credentials_are_invalid(
    client_anonymous,
    db_session,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)
    db_session.add(
        PlatformSetting(
            qz_certificate_encrypted="not-a-valid-token",
            qz_private_key_encrypted="not-a-valid-token",
        )
    )
    db_session.commit()

    certificate_response = client_anonymous.get("/qz/certificate")
    sign_response = client_anonymous.post("/qz/sign", json={"request": "demo-request"})

    assert certificate_response.status_code == 503
    assert certificate_response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE
    assert "decrypted" not in certificate_response.text.lower()
    assert sign_response.status_code == 503
    assert sign_response.text == qz_signing.QZ_PUBLIC_UNAVAILABLE_MESSAGE
    assert "decrypted" not in sign_response.text.lower()


def test_qz_signing_diagnostics_report_ready_when_saved_platform_credentials_exist(
    db_session,
    monkeypatch,
    tmp_path,
):
    _certificate_path, private_key_path, certificate_text = _write_qz_test_keys(tmp_path)
    _clear_qz_signing_config(monkeypatch)
    save_platform_qz_credentials(
        db_session,
        certificate_text=certificate_text,
        private_key_text=private_key_path.read_text(encoding="utf-8"),
    )
    db_session.commit()

    diagnostics = qz_signing.build_qz_signing_diagnostics(enabled=True, db=db_session)

    assert diagnostics.certificate.configured is True
    assert diagnostics.private_key.configured is True
    assert diagnostics.certificate.source_type == "platform_setting"
    assert diagnostics.private_key.source_type == "platform_setting"
    assert diagnostics.certificate_route.ok is True
    assert diagnostics.sign_route.ok is True
    assert diagnostics.signing_operational is True
    assert diagnostics.ready_for_tenants is True


def test_qz_signing_service_loads_dev_demo_keys_from_desktop(monkeypatch, tmp_path):
    certificate_dir = tmp_path / "Desktop" / "QZ Tray Demo Cert"
    certificate_dir.mkdir(parents=True, exist_ok=True)
    _certificate_path, _private_key_path, certificate_text = _write_qz_test_keys(certificate_dir)

    _clear_qz_signing_config(monkeypatch)
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(qz_signing.Path, "home", lambda: tmp_path)

    assert qz_signing.load_qz_certificate_text() == certificate_text.strip()
    assert qz_signing.sign_qz_message("demo-request")


def test_qz_print_js_uses_signed_certificate_and_signature_promises():
    script = Path("app/static/js/qz_print.js").read_text(encoding="utf-8")

    assert "qz.security.setCertificatePromise" in script
    assert 'qz.security.setSignatureAlgorithm("SHA512")' in script
    assert "qz.security.setSignaturePromise" in script
