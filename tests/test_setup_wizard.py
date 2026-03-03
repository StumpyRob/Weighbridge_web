from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.models import Base, CompanySetting, PrintDestination, PrintTemplate, User, Yard
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


def _client_for_app(*, app: FastAPI, db_path: Path) -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, base_url="https://testserver")
    return client, SessionLocal


def _login_superadmin(client: TestClient, *, email: str, password: str) -> str:
    warm = client.get("/login")
    assert warm.status_code == 200
    csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "next": "/setup",
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def test_setup_runs_once_for_superadmin_and_then_disables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup.db")
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.commit()

        csrf = _login_superadmin(
            client, email="owner@example.com", password="SetupPass123!"
        )

        setup_get = client.get("/setup")
        assert setup_get.status_code == 200
        assert "Company Setup" in setup_get.text
        assert "Create default print templates and print destinations" not in setup_get.text
        assert "Seed demo lookups" not in setup_get.text

        setup_post = client.post(
            "/setup",
            data={
                "company_name": "Setup Co Ltd",
                "default_yard_name": "Primary Yard",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert setup_post.status_code == 303
        assert setup_post.headers.get("location") == "/"

        with SessionLocal() as db:
            company = db.execute(
                select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
            ).scalars().first()
            yard = db.execute(select(Yard).order_by(Yard.id.asc()).limit(1)).scalars().first()
            assert company is not None
            assert bool(company.is_initialized) is True
            assert company.name == "Setup Co Ltd"
            assert yard is not None
            assert str(yard.description or "").strip() == "Primary Yard"
            default_template = db.execute(select(PrintTemplate.id).limit(1)).scalar_one_or_none()
            default_destination = db.execute(
                select(PrintDestination.id).limit(1)
            ).scalar_one_or_none()
            assert default_template is not None
            assert default_destination is not None

        disabled = client.get("/setup")
        assert disabled.status_code == 404
    finally:
        client.close()


def test_empty_db_bootstrap_then_setup_works_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-empty-db-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-empty.db")
    try:
        bootstrap_get = client.get("/bootstrap")
        assert bootstrap_get.status_code == 200
        csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert csrf

        bootstrap_post = client.post(
            "/bootstrap",
            data={
                "email": "owner@example.com",
                "password": "SetupPass123!",
                "confirm_password": "SetupPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert bootstrap_post.status_code == 303
        assert bootstrap_post.headers.get("location") == "/login?bootstrap=1"

        csrf = _login_superadmin(
            client, email="owner@example.com", password="SetupPass123!"
        )

        setup_get = client.get("/setup")
        assert setup_get.status_code == 200

        setup_post = client.post(
            "/setup",
            data={
                "company_name": "Setup Co Ltd",
                "default_yard_name": "Primary Yard",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert setup_post.status_code == 303
        assert setup_post.headers.get("location") == "/"

        with SessionLocal() as db:
            company = db.execute(
                select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
            ).scalars().first()
            assert company is not None
            assert bool(company.is_initialized) is True

        disabled = client.get("/setup")
        assert disabled.status_code == 404
    finally:
        client.close()


def test_setup_redirects_unauthenticated_user_to_login(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-login-redirect-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, _SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-login.db")
    try:
        response = client.get("/setup", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers.get("location") == "/login?next=/setup"
    finally:
        client.close()


def test_uninitialized_guard_returns_503_without_auto_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-guard-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-guard.db")
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.commit()
            existing_company = db.execute(
                select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
            ).scalars().first()
            assert existing_company is None

        _login_superadmin(client, email="owner@example.com", password="SetupPass123!")

        response = client.get("/tickets", follow_redirects=False)
        assert response.status_code == 503
        assert "System Setup Required" in response.text
        assert "/setup" in response.text

        with SessionLocal() as db:
            existing_company = db.execute(
                select(CompanySetting).order_by(CompanySetting.id.asc()).limit(1)
            ).scalars().first()
            assert existing_company is None
    finally:
        client.close()


def test_key_pages_do_not_500_after_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-pages-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-pages.db")
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.commit()

        csrf = _login_superadmin(
            client, email="owner@example.com", password="SetupPass123!"
        )
        setup_post = client.post(
            "/setup",
            data={
                "company_name": "Setup Co Ltd",
                "default_yard_name": "Primary Yard",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert setup_post.status_code == 303

        for path in ("/tickets", "/customers", "/invoices"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code < 500, path
    finally:
        client.close()
