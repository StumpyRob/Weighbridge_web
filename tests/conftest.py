import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_SECRET_KEY", "test-suite-secret-key-1234567890")

from app.auth import ROLE_SUPERADMIN, hash_password, user_identity_kwargs
from app.db import get_db
from app.main import app
from app.models import Base, User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    upsert_default_yard,
)


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def SessionLocal(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture()
def db_session(SessionLocal):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _prime_csrf(client: TestClient) -> str:
    client.get("/login")
    csrf = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    if csrf:
        client.headers.update({CSRF_HEADER_NAME: csrf})
    return csrf


def _login_superadmin(client: TestClient, *, email: str, password: str) -> None:
    csrf = _prime_csrf(client)
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
    assert response.status_code in (302, 303)
    _prime_csrf(client)


@pytest.fixture()
def client_anonymous(SessionLocal):
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, base_url="https://testserver") as test_client:
        _prime_csrf(test_client)
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def client_logged_in_no_setup(SessionLocal, client_anonymous):
    email = "test-superadmin@example.com"
    password = "TestPass123!"
    with SessionLocal() as db:
        db.add(
            User(
                **user_identity_kwargs(email=email, role=ROLE_SUPERADMIN),
                password_hash=hash_password(password),
                is_active=True,
            )
        )
        db.commit()

    _login_superadmin(client_anonymous, email=email, password=password)
    return client_anonymous


@pytest.fixture()
def client_logged_in_superadmin(SessionLocal, client_logged_in_no_setup):
    with SessionLocal() as db:
        company = ensure_company_settings_row_exists(db)
        company.name = "Your Company Name"
        company.is_initialized = True
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        db.commit()

    return client_logged_in_no_setup


@pytest.fixture()
def client(client_logged_in_superadmin):
    return client_logged_in_superadmin
