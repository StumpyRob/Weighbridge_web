from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password, user_identity_kwargs
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.models import Base, CompanySetting, User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME


def _client_for_app(
    *,
    app: FastAPI,
    db_path: Path,
    base_url: str = "https://testserver",
    raise_server_exceptions: bool = True,
    system_initialized: bool = False,
    authenticated: bool = False,
) -> TestClient:
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    if system_initialized:
        with SessionLocal() as db:
            db.add(CompanySetting(name="Security Test Co", is_initialized=True))
            if authenticated:
                db.add(
                    User(
                        **user_identity_kwargs(email="security@example.com"),
                        password_hash=hash_password("TestPass123!"),
                        is_active=True,
                    )
                )
            db.commit()

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(
        app,
        base_url=base_url,
        raise_server_exceptions=raise_server_exceptions,
    )
    if authenticated:
        warm = client.get("/login")
        assert warm.status_code == 200
        csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert csrf
        login = client.post(
            "/login",
            data={
                "email": "security@example.com",
                "password": "TestPass123!",
                "next": "/customers",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert login.status_code in (302, 303)
    return client


def test_create_app_fails_without_secret_when_non_dev(monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "")
    monkeypatch.setattr(settings, "secret_key", "")

    with pytest.raises(RuntimeError):
        create_app(dev_mode=False)


def test_csrf_is_required_for_post_requests_in_non_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "csrf-prod-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)

    with _client_for_app(
        app=app,
        db_path=tmp_path / "csrf.db",
        system_initialized=True,
        authenticated=True,
    ) as client:
        blocked = client.post(
            "/customers/new",
            data={"account_code": "C-CSRF-01", "name": "CSRF Customer"},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
        assert "CSRF" in blocked.text

        warm = client.get("/customers/new")
        assert warm.status_code == 200
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        assert csrf_token

        allowed = client.post(
            "/customers/new",
            data={
                "account_code": "C-CSRF-01",
                "name": "CSRF Customer",
                CSRF_FORM_FIELD: csrf_token,
            },
            headers={CSRF_HEADER_NAME: csrf_token},
            follow_redirects=False,
        )
        assert allowed.status_code == 303


def test_security_headers_are_present(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "headers-prod-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)

    with _client_for_app(app=app, db_path=tmp_path / "headers.db") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("referrer-policy") == "same-origin"
        assert "geolocation=()" in str(response.headers.get("permissions-policy", ""))
        assert response.headers.get("x-frame-options") == "DENY"
        csp = str(response.headers.get("content-security-policy", ""))
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


def test_csrf_cookie_has_secure_defaults_on_https(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "cookie-prod-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)

    with _client_for_app(
        app=app,
        db_path=tmp_path / "cookies.db",
        base_url="https://testserver",
    ) as client:
        response = client.get("/")
        assert response.status_code == 200
        set_cookie = str(response.headers.get("set-cookie", ""))
        set_cookie_lower = set_cookie.lower()
        assert f"{CSRF_COOKIE_NAME}=" in set_cookie
        assert "httponly" in set_cookie_lower
        assert "samesite=lax" in set_cookie_lower
        assert "secure" in set_cookie_lower


def test_internal_errors_do_not_leak_tracebacks_in_non_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "errors-prod-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("sensitive debug details")

    with _client_for_app(
        app=app,
        db_path=tmp_path / "errors.db",
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/boom")
        assert response.status_code == 500
        assert "Traceback" not in response.text
        assert "sensitive debug details" not in response.text
        assert "Internal Server Error" in response.text

