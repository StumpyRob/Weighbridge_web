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
from app.services import qz_signing


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
    assert "QZ signing certificate is not configured." in certificate_response.text
    assert sign_response.status_code == 503
    assert "QZ signing private key is not configured." in sign_response.text


def test_qz_certificate_route_returns_503_with_admin_config_error_when_missing(
    client_anonymous,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)

    response = client_anonymous.get("/qz/certificate")

    assert response.status_code == 503
    assert "QZ signing certificate is not configured." in response.text
    assert "QZ_CERTIFICATE_TEXT" in response.text
    assert "QZ_CERTIFICATE_PATH" in response.text
    assert "QZ_CERTIFICATE_FILE" in response.text
    assert "$RAILWAY_VOLUME_MOUNT_PATH/qz/digital-certificate.txt" in response.text


def test_qz_sign_route_returns_503_with_admin_config_error_when_missing(
    client_anonymous,
    monkeypatch,
):
    _clear_qz_signing_config(monkeypatch)

    response = client_anonymous.post("/qz/sign", json={"request": "demo-request"})

    assert response.status_code == 503
    assert "QZ signing private key is not configured." in response.text
    assert "QZ_PRIVATE_KEY_TEXT" in response.text
    assert "QZ_PRIVATE_KEY_PATH" in response.text
    assert "QZ_PRIVATE_KEY_FILE" in response.text
    assert "$RAILWAY_VOLUME_MOUNT_PATH/qz/private-key.pem" in response.text


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
