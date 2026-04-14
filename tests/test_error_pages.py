from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import Base, CompanySetting, Tenant
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


def test_unknown_tenant_route_renders_html_not_found_page(tmp_path, monkeypatch):
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

    with TestClient(app, base_url="https://ghost.localhost") as client:
        response = client.get("/tickets")

    assert response.status_code == 404
    assert 'class="marketing-landing-page marketing-error-page"' in response.text
    assert "Workspace Not Found" in response.text
    assert "ghost" in response.text
    assert "Requested path" in response.text
    assert "Open Platform" in response.text
    assert "Try the Demo" in response.text
    assert "Visit the Site" in response.text
    assert "Tenant One Branding" not in response.text


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
    assert "Workspace Home" in response.text


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
