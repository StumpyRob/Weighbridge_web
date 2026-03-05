from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_USER, hash_password, user_identity_kwargs
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.models import (
    Base,
    CompanySetting,
    PaymentMethod,
    PrintDestination,
    PrintTemplate,
    TaxRate,
    Unit,
    User,
    VehicleType,
    VoidReason,
    Yard,
)
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


def test_protected_pages_redirect_to_login_when_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-auth-redirects-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(
        app=app, db_path=tmp_path / "setup-auth-redirects.db"
    )
    try:
        tickets = client.get("/tickets", follow_redirects=False)
        assert tickets.status_code == 302
        assert tickets.headers.get("location") == "/login?next=/tickets"

        admin = client.get("/admin", follow_redirects=False)
        assert admin.status_code == 302
        assert admin.headers.get("location") == "/login?next=/admin"

        home = client.get("/")
        assert home.status_code == 200

        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.add(CompanySetting(name="Setup Co Ltd", is_initialized=True))
            db.commit()

        _login_superadmin(client, email="owner@example.com", password="SetupPass123!")
        tickets_after_login = client.get("/tickets")
        assert tickets_after_login.status_code == 200
        admin_after_login = client.get("/admin")
        assert admin_after_login.status_code == 200
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

        with SessionLocal() as db:
            assert db.execute(select(Unit.id).limit(1)).scalar_one_or_none() is not None
            assert db.execute(select(TaxRate.id).limit(1)).scalar_one_or_none() is not None
            assert (
                db.execute(select(VehicleType.id).limit(1)).scalar_one_or_none()
                is not None
            )
            assert (
                db.execute(select(PaymentMethod.id).limit(1)).scalar_one_or_none()
                is not None
            )
            assert (
                db.execute(
                    select(VoidReason.id).where(VoidReason.reason_type == "TICKET")
                ).first()
                is not None
            )
            assert (
                db.execute(
                    select(VoidReason.id).where(VoidReason.reason_type == "INVOICE")
                ).first()
                is not None
            )

        expected_status = {
            "/products": 200,
            "/products/new": 200,
            "/vehicles/new": 200,
            "/tickets/new": 200,
            "/lookups": 200,
            "/lookups/hauliers": 200,
        }
        for path, status_code in expected_status.items():
            response = client.get(path, follow_redirects=True)
            assert response.status_code == status_code, path
    finally:
        client.close()


def test_system_status_superadmin_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-system-status-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-status.db")
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.add(
                User(
                    **user_identity_kwargs(email="ops@example.com", role=ROLE_USER),
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
                "company_name": "Status Co Ltd",
                "default_yard_name": "Primary Yard",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert setup_post.status_code == 303

        status_page = client.get("/admin/system-status")
        assert status_page.status_code == 200
        assert "System Status" in status_page.text
        assert "Initialized:</strong> Yes" in status_page.text

        logout = client.post(
            "/logout",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert logout.status_code == 303

        _login_superadmin(client, email="ops@example.com", password="SetupPass123!")
        forbidden = client.get("/admin/system-status")
        assert forbidden.status_code == 403
    finally:
        client.close()


def test_navbar_shows_signed_in_indicator_and_sign_in_link(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-navbar-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-navbar.db")
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
        signed_in_page = client.get("/admin")
        assert signed_in_page.status_code == 200
        assert "Signed in as owner@example.com" in signed_in_page.text

        logout = client.post(
            "/logout",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert logout.status_code == 303

        signed_out_page = client.get("/admin", follow_redirects=False)
        assert signed_out_page.status_code == 302
        assert signed_out_page.headers.get("location") == "/login?next=/admin"
    finally:
        client.close()


def test_lookups_page_shows_readiness_warning_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-lookups-ready-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-lookups.db")
    try:
        with SessionLocal() as db:
            db.add(
                User(
                    username="owner@example.com",
                    password_hash=hash_password("SetupPass123!"),
                    is_active=True,
                )
            )
            db.add(CompanySetting(name="Setup Co Ltd", is_initialized=True))
            db.commit()

        _login_superadmin(client, email="owner@example.com", password="SetupPass123!")
        response = client.get("/lookups/hauliers")
        assert response.status_code == 200
        assert "System not fully configured." in response.text
    finally:
        client.close()


def test_home_first_time_setup_panel_visibility(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-home-panel-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-home.db")
    try:
        initial = client.get("/")
        assert initial.status_code == 200
        assert "First-time Setup" in initial.text

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

        ready = client.get("/")
        assert ready.status_code == 200
        assert "First-time Setup" not in ready.text
        assert "Setup complete. System initialization checks are green." in ready.text
    finally:
        client.close()


def test_home_shows_setup_panel_when_initialized_but_required_lookups_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "app_secret_key", "setup-wizard-home-missing-lookups")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "setup-home-missing.db")
    try:
        with SessionLocal() as db:
            db.add(CompanySetting(name="Setup Co Ltd", is_initialized=True))
            db.commit()

        response = client.get("/")
        assert response.status_code == 200
        assert "First-time Setup" in response.text
        assert "Required reference data is incomplete." in response.text
    finally:
        client.close()
