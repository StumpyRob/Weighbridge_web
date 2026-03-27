from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import Base, PlatformSetting, Tenant, User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.seed import seed_print_destinations, seed_print_templates
from app.services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from app.tenancy import current_platform_mode, current_tenant_id


def _build_app_and_session(
    tmp_path: Path,
    *,
    db_name: str,
    monkeypatch,
) -> tuple[FastAPI, sessionmaker]:
    monkeypatch.setattr(settings, "app_secret_key", "platform-qz-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")

    app = create_app(dev_mode=False)
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        class_=TenantSession,
    )

    def override_get_db():
        db = SessionLocal()
        db.info["tenant_id"] = current_tenant_id()
        db.info["platform_mode"] = current_platform_mode()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return app, SessionLocal


def _prime_csrf(client: TestClient, *, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    client.headers.update({CSRF_HEADER_NAME: token})
    return token


def _login(client: TestClient, *, email: str, password: str, next_path: str) -> None:
    login_path = "/login"
    if next_path.startswith("/platform/"):
        login_path = f"/login?next={next_path}"
    csrf = _prime_csrf(client, path=login_path)
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "next": next_path,
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    _prime_csrf(client)


def _seed_tenant(SessionLocal: sessionmaker, *, name: str, subdomain: str) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _seed_tenant_baseline(SessionLocal: sessionmaker, *, tenant_id: int, company_name: str) -> None:
    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_id
        db.info["platform_mode"] = False
        company = ensure_company_settings_row_exists(db)
        company.tenant_id = tenant_id
        company.name = company_name
        company.is_initialized = True
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        seed_required_reference_data(db)
        seed_print_templates(db)
        seed_print_destinations(db)
        db.commit()


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: int | None,
) -> None:
    with SessionLocal() as db:
        db.add(
            User(
                **user_identity_kwargs(email=email, role=role),
                password_hash=hash_password(password),
                is_active=True,
                tenant_id=tenant_id,
            )
        )
        db.commit()


def _build_qz_test_credentials() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "GB"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Weighbridge Web Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Platform QZ Settings Test"),
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
    certificate_text = certificate.public_bytes(Encoding.PEM).decode("utf-8")
    private_key_text = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")
    return certificate_text, private_key_text


def test_platform_qz_settings_page_reports_missing_status_and_routes(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-page.db",
        monkeypatch=monkeypatch,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login(
            client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/qz-settings",
        )
        response = client.get("/platform/qz-settings")

    assert response.status_code == 200
    assert ">Platform QZ Settings<" in response.text
    assert "QZ Printing Not Configured" in response.text
    assert (
        "Direct workstation printing will not work until a certificate and private key are configured."
        in response.text
    )
    assert "Add a QZ certificate and private key below." in response.text
    assert "QZ signing is configured once at platform level." in response.text
    assert "Tenant admins only control which printers are used." in response.text
    assert "Save Credentials" in response.text
    assert "Certificate is not configured." in response.text
    assert "Private Key is not configured." in response.text
    assert "Advanced diagnostics" in response.text
    assert "Show technical details" in response.text
    assert "/qz/certificate" in response.text
    assert "/qz/sign" in response.text
    assert "Ready for tenant use" not in response.text
    assert "Signing operational" not in response.text


def test_platform_qz_settings_page_uses_ready_banner_when_operational(tmp_path, monkeypatch):
    import app.routes.superadmin as superadmin_routes

    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-ready.db",
        monkeypatch=monkeypatch,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    monkeypatch.setattr(
        superadmin_routes,
        "build_qz_signing_diagnostics",
        lambda *, enabled, db=None: SimpleNamespace(
            enabled=enabled,
            certificate=SimpleNamespace(
                ok=True,
                configured=True,
                summary="Certificate is configured.",
                next_step="",
                source_type="platform_setting",
                validation_status="ok",
                source_label="Environment inline value (QZ_CERTIFICATE_TEXT)",
                resolved_path=None,
                detail="",
                checked_paths=(),
            ),
            private_key=SimpleNamespace(
                ok=True,
                configured=True,
                summary="Private Key is configured.",
                next_step="",
                source_type="platform_setting",
                validation_status="ok",
                source_label="Environment inline value (QZ_PRIVATE_KEY_TEXT)",
                resolved_path=None,
                detail="",
                checked_paths=(),
            ),
            certificate_route=SimpleNamespace(
                ok=True,
                status="ok",
                summary="OK",
                detail="Certificate route can return the configured certificate.",
            ),
            sign_route=SimpleNamespace(
                ok=True,
                status="ok",
                summary="OK",
                detail="Sign route can produce SHA-512 signatures.",
            ),
            csp_connect_src_ok=True,
            csp_detail="CSP allows secure QZ Tray websocket endpoints.",
            likely_causes=(),
            browser_requirements=(),
            signing_operational=True,
            ready_for_tenants=True,
        ),
    )

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login(
            client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/qz-settings",
        )
        response = client.get("/platform/qz-settings")

    assert response.status_code == 200
    assert "QZ Printing Ready" in response.text
    assert "Direct workstation printing is operational." in response.text


def test_platform_qz_settings_update_and_validate_persist_status(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-update.db",
        monkeypatch=monkeypatch,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login(
            client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/qz-settings",
        )
        csrf = _prime_csrf(client, path="/platform/qz-settings")
        disable = client.post(
            "/platform/qz-settings",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert disable.status_code == 303
        assert disable.headers.get("location") == "/platform/qz-settings?saved=1"

        csrf = _prime_csrf(client, path="/platform/qz-settings")
        validate = client.post(
            "/platform/qz-settings/validate",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert validate.status_code == 303
        assert validate.headers.get("location") == "/platform/qz-settings?validated=1"

        validated_page = client.get(validate.headers["location"])

    assert validated_page.status_code == 200
    assert "QZ validation completed." in validated_page.text
    assert "QZ Printing Disabled" in validated_page.text
    assert "Direct workstation printing is turned off at platform level." in validated_page.text

    with SessionLocal() as db:
        state = db.query(PlatformSetting).order_by(PlatformSetting.id.asc()).first()
        assert state is not None
        assert bool(state.qz_enabled) is False
        assert str(state.qz_last_validation_status or "").lower() == "error"
        assert "QZ printing is disabled at platform level." in str(
            state.qz_last_validation_summary or ""
        )


def test_superadmin_can_save_platform_qz_credentials_without_rendering_private_key(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-credentials.db",
        monkeypatch=monkeypatch,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    certificate_text, private_key_text = _build_qz_test_credentials()

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login(
            client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/qz-settings",
        )
        csrf = _prime_csrf(client, path="/platform/qz-settings")
        response = client.post(
            "/platform/qz-settings/credentials",
            data={
                CSRF_FORM_FIELD: csrf,
                "certificate_text": certificate_text,
                "private_key_text": private_key_text,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get("/platform/qz-settings")

    assert page.status_code == 200
    assert "Private key is write-only and is never shown back after save." in page.text
    assert certificate_text.strip() not in page.text
    assert private_key_text.strip() not in page.text
    assert "Currently using the saved platform certificate." in page.text
    assert "Currently using the saved platform private key." in page.text

    with SessionLocal() as db:
        state = db.query(PlatformSetting).order_by(PlatformSetting.id.asc()).first()
        assert state is not None
        assert bool(state.qz_certificate_encrypted)
        assert bool(state.qz_private_key_encrypted)
        assert state.qz_certificate_updated_at is not None
        assert state.qz_private_key_updated_at is not None


def test_platform_qz_validate_uses_saved_credentials(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-validate-stored.db",
        monkeypatch=monkeypatch,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    certificate_text, private_key_text = _build_qz_test_credentials()

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login(
            client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/qz-settings",
        )
        csrf = _prime_csrf(client, path="/platform/qz-settings")
        save_response = client.post(
            "/platform/qz-settings/credentials",
            data={
                CSRF_FORM_FIELD: csrf,
                "certificate_text": certificate_text,
                "private_key_text": private_key_text,
            },
            follow_redirects=False,
        )
        assert save_response.status_code == 303

        csrf = _prime_csrf(client, path="/platform/qz-settings")
        validate_response = client.post(
            "/platform/qz-settings/validate",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert validate_response.status_code == 303
        page = client.get(validate_response.headers["location"])

    assert page.status_code == 200
    assert "QZ validation completed." in page.text

    with SessionLocal() as db:
        state = db.query(PlatformSetting).order_by(PlatformSetting.id.asc()).first()
        assert state is not None
        assert str(state.qz_last_validation_status or "").lower() == "ok"
        assert "operational" in str(state.qz_last_validation_summary or "").lower()


def test_tenant_admin_cannot_access_platform_qz_settings_pages(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-permissions.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_tenant_baseline(SessionLocal, tenant_id=tenant_id, company_name="Acme")
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(
            client,
            email="tenant-admin@example.com",
            password="TestPass123!",
            next_path="/admin",
        )
        platform_page = client.get("/platform/qz-settings")
        admin_alias_page = client.get("/admin/qz-settings")
        csrf = _prime_csrf(client, path="/admin")
        credential_update = client.post(
            "/platform/qz-settings/credentials",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )

    assert platform_page.status_code == 404
    assert admin_alias_page.status_code == 404
    assert credential_update.status_code == 404


def test_tenant_admin_can_configure_destination_behavior_without_secret_visibility(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="platform-qz-tenant-admin.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_tenant_baseline(SessionLocal, tenant_id=tenant_id, company_name="Acme")
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(
            client,
            email="tenant-admin@example.com",
            password="TestPass123!",
            next_path="/admin/printing/destinations/new",
        )
        response = client.get("/admin/printing/destinations/new")

    assert response.status_code == 200
    assert "Print directly to workstation printer" in response.text
    assert "Printer name (optional)" in response.text
    assert "Leave the printer name blank to use the workstation default printer." in response.text
    assert "QZ_CERTIFICATE_TEXT" not in response.text
    assert "QZ_PRIVATE_KEY_TEXT" not in response.text
    assert "/qz/certificate" not in response.text
    assert "/qz/sign" not in response.text
