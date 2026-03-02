from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_ADMIN, ROLE_OPERATOR, hash_password
from app.cli import create_superadmin_account
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.models import Base, Customer, User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME


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


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    role: str,
    is_active: bool = True,
) -> None:
    with SessionLocal() as db:
        db.add(
            User(
                email=email,
                password_hash=hash_password(password),
                role=role,
                is_active=is_active,
            )
        )
        db.commit()


def _login(client: TestClient, *, email: str, password: str) -> tuple[int, str]:
    warm = client.get("/login")
    assert warm.status_code == 200
    csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "next": "/tickets",
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    if response.status_code in {302, 303}:
        client.headers.update({CSRF_HEADER_NAME: csrf})
    return response.status_code, csrf


def test_login_success_and_fail_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "auth-login-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)

    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "auth-login.db")
    try:
        _seed_user(
            SessionLocal,
            email="admin@example.com",
            password="TestPass123!",
            role=ROLE_ADMIN,
        )
        warm = client.get("/login")
        assert warm.status_code == 200
        bad_csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert bad_csrf

        bad = client.post(
            "/login",
            data={
                "email": "admin@example.com",
                "password": "wrong-pass",
                CSRF_FORM_FIELD: bad_csrf,
            },
            follow_redirects=False,
        )
        assert bad.status_code == 401
        assert "Invalid email or password." in bad.text

        ok_status, _csrf = _login(
            client,
            email="admin@example.com",
            password="TestPass123!",
        )
        assert ok_status == 303

        tickets = client.get("/tickets")
        assert tickets.status_code == 200
    finally:
        client.close()


def test_operator_is_blocked_from_admin_only_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "auth-roles-test-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "auth-roles.db")
    try:
        _seed_user(
            SessionLocal,
            email="operator@example.com",
            password="TestPass123!",
            role=ROLE_OPERATOR,
        )
        with SessionLocal() as db:
            db.add(Customer(account_code="C-AUTH-1", name="Auth Customer"))
            db.commit()

        login_status, csrf = _login(
            client,
            email="operator@example.com",
            password="TestPass123!",
        )
        assert login_status == 303

        adjustment = client.post(
            "/customers/1/adjustments",
            data={
                "amount_decimal": "10.00",
                "reason": "GOODWILL_CREDIT",
                "note": "Role restriction test",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert adjustment.status_code == 403

        customer_flags = client.post(
            "/customers/new",
            data={
                "account_code": "C-AUTH-2",
                "name": "Flagged Customer",
                "on_stop": "on",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert customer_flags.status_code == 403

        printing = client.get("/admin/printing/templates")
        assert printing.status_code == 403
    finally:
        client.close()


def test_bootstrap_superadmin_only_once(tmp_path):
    db_path = tmp_path / "bootstrap.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    created = create_superadmin_account(
        email="first@example.com",
        password="TestPass123!",
        session_factory=SessionLocal,
    )
    assert created.email == "first@example.com"
    assert created.role == "SUPERADMIN"

    with pytest.raises(RuntimeError):
        create_superadmin_account(
            email="second@example.com",
            password="TestPass123!",
            session_factory=SessionLocal,
        )


def test_web_bootstrap_creates_superadmin_and_disables_route(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "auth-web-bootstrap-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(app=app, db_path=tmp_path / "web-bootstrap.db")
    try:
        bootstrap_get = client.get("/bootstrap")
        assert bootstrap_get.status_code == 200
        assert "Create First Superadmin" in bootstrap_get.text

        csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert csrf
        bootstrap_post = client.post(
            "/bootstrap",
            data={
                "email": "first-admin@example.com",
                "password": "TestPass123!",
                "confirm_password": "TestPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert bootstrap_post.status_code == 303
        assert bootstrap_post.headers.get("location") == "/login?bootstrap=1"

        with SessionLocal() as db:
            created = db.query(User).filter(User.email == "first-admin@example.com").first()
            assert created is not None
            assert created.role == "SUPERADMIN"
            assert created.is_active

        disabled = client.get("/bootstrap")
        assert disabled.status_code == 404

        login_status, _csrf = _login(
            client,
            email="first-admin@example.com",
            password="TestPass123!",
        )
        assert login_status == 303
        tickets = client.get("/tickets")
        assert tickets.status_code == 200
    finally:
        client.close()


def test_web_bootstrap_is_404_when_users_already_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "auth-web-bootstrap-disabled-secret")
    monkeypatch.setattr(settings, "secret_key", "")
    app = create_app(dev_mode=False)
    client, SessionLocal = _client_for_app(
        app=app, db_path=tmp_path / "web-bootstrap-disabled.db"
    )
    try:
        _seed_user(
            SessionLocal,
            email="existing@example.com",
            password="TestPass123!",
            role=ROLE_ADMIN,
        )

        get_response = client.get("/bootstrap")
        assert get_response.status_code == 404

        warm_login = client.get("/login")
        assert warm_login.status_code == 200
        csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert csrf

        post_response = client.post(
            "/bootstrap",
            data={
                "email": "another@example.com",
                "password": "TestPass123!",
                "confirm_password": "TestPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert post_response.status_code == 404
    finally:
        client.close()
