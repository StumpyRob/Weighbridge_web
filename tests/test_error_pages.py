from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_TENANT_ADMIN, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import Base, CompanySetting, Tenant, User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from app.tenancy import current_platform_mode, current_tenant_id


def _build_app_and_session(tmp_path: Path, *, db_name: str, monkeypatch) -> tuple[FastAPI, sessionmaker]:
    monkeypatch.setattr(settings, "app_secret_key", "error-page-test-secret")
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


def _seed_tenant(
    SessionLocal: sessionmaker,
    *,
    name: str,
    subdomain: str,
    company_name: str | None = None,
) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=True)
        db.add(tenant)
        db.flush()
        db.add(
            CompanySetting(
                tenant_id=int(tenant.id),
                name=company_name or name,
                is_initialized=True,
            )
        )
        db.commit()
        return int(tenant.id)


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    tenant_id: int,
) -> int:
    with SessionLocal() as db:
        user = User(
            **user_identity_kwargs(email=email, role=ROLE_TENANT_ADMIN),
            tenant_id=tenant_id,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.commit()
        return int(user.id)


def _prime_csrf(client: TestClient, *, login_path: str = "/login") -> str:
    response = client.get(login_path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    return token


def _login(client: TestClient, *, email: str, password: str, next_path: str = "/tickets") -> int:
    login_path = f"/login?{urlencode({'next': next_path})}"
    csrf = _prime_csrf(client, login_path=login_path)
    response = client.post(
        "/login",
        data={
            CSRF_FORM_FIELD: csrf,
            "email": email,
            "password": password,
            "next": next_path,
        },
        follow_redirects=False,
    )
    return response.status_code


def test_unknown_tenant_route_redirects_to_main_landing_page(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="unknown-tenant-error.db",
        monkeypatch=monkeypatch,
    )
    _seed_tenant(
        SessionLocal,
        name="Tenant One",
        subdomain="tenant-one",
        company_name="Tenant One Branding",
    )

    with TestClient(app, base_url="https://ghost.example.test") as client:
        response = client.get("/tickets", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://example.test/"


def test_missing_page_on_tenant_host_uses_tenant_branding(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-not-found-page.db",
        monkeypatch=monkeypatch,
    )
    _seed_tenant(
        SessionLocal,
        name="Tenant One",
        subdomain="tenant-one",
        company_name="Tenant One Branding",
    )

    with TestClient(app, base_url="https://tenant-one.localhost") as client:
        response = client.get("/missing-page")

    assert response.status_code == 404
    assert "<h1>Page Not Found</h1>" in response.text
    assert "Tenant One Branding" in response.text
    assert "/missing-page" in response.text
    assert "Workspace" in response.text
    assert "Host" in response.text
    assert "Requested path" in response.text
    assert "What to do next" in response.text
    assert "Workspace Home" not in response.text
    assert "Open Tickets" not in response.text
    assert 'class="btn btn--secondary error-page-home-button"' in response.text


def test_signed_in_tenant_missing_page_shows_report_bug_action(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-not-found-feedback.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Tenant One",
        subdomain="tenant-one",
        company_name="Tenant One Branding",
    )
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TestPass123!",
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://tenant-one.localhost") as client:
        assert _login(
            client,
            email="tenant-admin@example.com",
            password="TestPass123!",
            next_path="/tickets",
        ) == 303
        response = client.get("/missing-page")

    assert response.status_code == 404
    assert 'class="btn btn--ghost error-page-report-button"' in response.text
    assert 'data-feedback-open' in response.text
    assert 'data-feedback-dialog' in response.text


def test_missing_page_on_marketing_host_uses_marketing_not_found_page(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, _SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="marketing-not-found-page.db",
        monkeypatch=monkeypatch,
    )

    with TestClient(app, base_url="https://software.example.test") as client:
        response = client.get("/missing-page")

    assert response.status_code == 404
    assert 'class="marketing-landing-page marketing-error-page"' in response.text
    assert "Page Not Found" in response.text
    assert "Visit the Site" in response.text
    assert "Try the Demo" in response.text
    assert "Open Platform" in response.text
    assert "/missing-page" in response.text
