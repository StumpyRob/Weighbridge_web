from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import AuditEvent, Base, CompanySetting, Customer, EwcCode, Product, Tenant, Ticket, User
from app.models.base import utcnow
from app.seed import seed_print_destinations, seed_print_templates
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
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
    monkeypatch.setattr(settings, "app_secret_key", "tenant-test-secret")
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


def _client(app: FastAPI, *, base_url: str) -> TestClient:
    return TestClient(app, base_url=base_url)


def _prime_csrf(client: TestClient, *, login_path: str = "/login") -> str:
    response = client.get(login_path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    return token


def _login(
    client: TestClient,
    *,
    email: str,
    password: str,
    next_path: str = "/tickets",
    login_path: str | None = None,
) -> int:
    csrf_login_path = login_path
    if csrf_login_path is None and next_path.startswith("/platform/"):
        csrf_login_path = f"/login?{urlencode({'next': next_path})}"
    csrf = _prime_csrf(client, login_path=csrf_login_path or "/login")
    login_submit_path = str(urlsplit(csrf_login_path or "/login").path or "/login")
    response = client.post(
        login_submit_path,
        data={
            "email": email,
            "password": password,
            "next": next_path,
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    return response.status_code


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: int | None,
) -> int:
    with SessionLocal() as db:
        user = User(
                **user_identity_kwargs(email=email, role=role),
                password_hash=hash_password(password),
                is_active=True,
                tenant_id=tenant_id,
            )
        db.add(user)
        db.commit()
        db.refresh(user)
        return int(user.id)


def _seed_company(SessionLocal: sessionmaker, *, tenant_id: int, name: str, primary_color: str) -> None:
    with SessionLocal() as db:
        db.add(
            CompanySetting(
                tenant_id=tenant_id,
                name=name,
                primary_color_hex=primary_color,
                is_initialized=True,
            )
        )
        db.commit()


def _seed_tenant_baseline(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    company_name: str,
    primary_color: str,
) -> None:
    with SessionLocal() as db:
        db.info["tenant_id"] = int(tenant_id)
        db.info["platform_mode"] = False
        company = ensure_company_settings_row_exists(db)
        company.tenant_id = int(tenant_id)
        company.name = company_name
        company.primary_color_hex = primary_color
        company.is_initialized = True
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        seed_required_reference_data(db)
        seed_print_templates(db)
        seed_print_destinations(db)
        db.commit()


def _seed_tenant(
    SessionLocal: sessionmaker,
    *,
    name: str,
    subdomain: str,
    is_active: bool = True,
) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=is_active)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def test_tenant_resolution_middleware(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-mw.db", monkeypatch=monkeypatch)
    active_id = _seed_tenant(SessionLocal, name="Company One", subdomain="company1", is_active=True)
    _ = active_id
    _seed_tenant(SessionLocal, name="Disabled Co", subdomain="disabled", is_active=False)

    with _client(app, base_url="https://company1.localhost") as valid:
        assert valid.get("/health").status_code == 200

    with _client(app, base_url="https://unknown.localhost") as unknown:
        response = unknown.get("/health")
        assert response.status_code == 404
        assert "Unknown tenant" in response.text

    with _client(app, base_url="https://disabled.localhost") as disabled:
        response = disabled.get("/health")
        assert response.status_code == 403
        assert "Tenant disabled" in response.text

    with _client(app, base_url="https://admin.localhost") as admin_host:
        response = admin_host.get("/platform/tenants", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert response.headers.get("location", "").startswith("/login")


def test_base_domain_serves_public_landing_and_subdomains_still_route(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-base-domain.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Company One",
        subdomain="company1",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Company One",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="company1-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://example.test") as base_client:
        landing = base_client.get("/")
        assert landing.status_code == 200
        assert "Weighbridge Web" in landing.text
        assert "Platform in development" in landing.text
        assert "Customers access the platform using their company subdomain" in landing.text

        blocked_tickets = base_client.get("/tickets")
        assert blocked_tickets.status_code == 404
        blocked_platform = base_client.get("/platform/tenants")
        assert blocked_platform.status_code == 404

    with _client(app, base_url="https://admin.example.test") as admin_client:
        platform_tenants = admin_client.get("/platform/tenants", follow_redirects=False)
        assert platform_tenants.status_code in {302, 303}
        assert platform_tenants.headers.get("location", "").startswith("/login")
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        assert admin_client.get("/platform/tenants").status_code == 200

    with _client(app, base_url="https://company1.example.test") as tenant_client:
        login_page = tenant_client.get("/login")
        assert login_page.status_code == 200
        assert _login(
            tenant_client,
            email="company1-admin@example.com",
            password="TestPass123!",
        ) == 303
        assert tenant_client.get("/tickets").status_code == 200

    with _client(app, base_url="https://unknown.example.test") as unknown_client:
        response = unknown_client.get("/health")
        assert response.status_code == 404
        assert "Unknown tenant" in response.text


def test_platform_bootstrap_on_admin_subdomain_creates_first_superadmin_without_breaking_tenant_access(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-platform-bootstrap.db", monkeypatch=monkeypatch
    )
    default_tenant = _seed_tenant(
        SessionLocal,
        name="Default Tenant",
        subdomain=settings.effective_default_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=default_tenant,
        company_name="Default Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="default-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=default_tenant,
    )

    with _client(app, base_url="https://admin.example.test") as platform_client:
        platform_entry = platform_client.get("/platform/tenants", follow_redirects=False)
        assert platform_entry.status_code in {302, 303}
        assert platform_entry.headers.get("location") == "/login?next=/platform/tenants"

        login_page = platform_client.get(platform_entry.headers["location"])
        assert login_page.status_code == 200
        assert "No user accounts exist yet." in login_page.text
        assert 'href="/platform/bootstrap"' in login_page.text

        bootstrap_page = platform_client.get("/platform/bootstrap")
        assert bootstrap_page.status_code == 200
        assert 'action="/platform/bootstrap"' in bootstrap_page.text

        csrf = str(platform_client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert csrf
        bootstrap_post = platform_client.post(
            "/platform/bootstrap",
            data={
                "email": "platform-owner@example.com",
                "password": "PlatformPass123!",
                "confirm_password": "PlatformPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert bootstrap_post.status_code == 303
        location = bootstrap_post.headers.get("location", "")
        assert location.startswith("/login?")
        assert "bootstrap=1" in location
        assert "next=%2Fplatform%2Ftenants" in location

        assert (
            _login(
                platform_client,
                email="platform-owner@example.com",
                password="PlatformPass123!",
                next_path="/platform/tenants",
            )
            == 303
        )
        assert platform_client.get("/platform/tenants").status_code == 200

        disabled_bootstrap = platform_client.get("/platform/bootstrap")
        assert disabled_bootstrap.status_code == 404

    with SessionLocal() as db:
        platform_owner = (
            db.execute(
                select(User).where(getattr(User, "email", getattr(User, "username")) == "platform-owner@example.com")
            )
            .scalars()
            .first()
        )
        default_admin = (
            db.execute(
                select(User).where(getattr(User, "email", getattr(User, "username")) == "default-admin@example.com")
            )
            .scalars()
            .first()
        )
        assert platform_owner is not None
        assert platform_owner.tenant_id is None
        assert str(platform_owner.role or "").strip().lower() == ROLE_SUPERADMIN
        assert default_admin is not None
        assert int(default_admin.tenant_id or 0) == default_tenant

    with _client(app, base_url=f"https://{settings.effective_default_tenant_subdomain}.example.test") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="default-admin@example.com",
                password="TenantPass123!",
            )
            == 303
        )
        assert tenant_client.get("/admin/company").status_code == 200
        assert tenant_client.get("/tickets").status_code == 200


def test_tenant_scoped_auth_rules(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-auth.db", monkeypatch=monkeypatch)
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    tenant_disabled = _seed_tenant(SessionLocal, name="Tenant Disabled", subdomain="disabled", is_active=False)

    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    _seed_user(
        SessionLocal,
        email="disabled-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_disabled,
    )
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#AA0000")
    _seed_company(SessionLocal, tenant_id=tenant_b, name="Tenant B Co", primary_color="#00AA00")
    _seed_company(SessionLocal, tenant_id=tenant_disabled, name="Disabled Co", primary_color="#0000AA")

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="a-admin@example.com", password="TestPass123!") == 401

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="superadmin@example.com", password="TestPass123!") == 401

    with _client(app, base_url="https://disabled.localhost") as disabled_client:
        assert disabled_client.get("/login").status_code == 403


def test_user_identity_uniqueness_is_tenant_scoped(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-user-unique.db", monkeypatch=monkeypatch
    )
    _ = app
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")

    with SessionLocal() as db:
        db.add(
            User(
                **user_identity_kwargs(email="shared@example.com", role=ROLE_TENANT_ADMIN),
                password_hash=hash_password("TenantAPass123!"),
                is_active=True,
                tenant_id=tenant_a,
            )
        )
        db.add(
            User(
                **user_identity_kwargs(email="shared@example.com", role=ROLE_TENANT_ADMIN),
                password_hash=hash_password("TenantBPass123!"),
                is_active=True,
                tenant_id=tenant_b,
            )
        )
        db.commit()

    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#AA1111")
    _seed_company(SessionLocal, tenant_id=tenant_b, name="Tenant B Co", primary_color="#11AA11")

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="shared@example.com", password="TenantAPass123!") == 303
    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="shared@example.com", password="TenantBPass123!") == 303
    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="shared@example.com", password="TenantAPass123!") == 401
    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="shared@example.com", password="TenantAPass123!") == 401

    with SessionLocal() as db:
        db.add(
            User(
                **user_identity_kwargs(email="shared@example.com", role=ROLE_TENANT_ADMIN),
                password_hash=hash_password("TenantADupePass123!"),
                is_active=True,
                tenant_id=tenant_a,
            )
        )
        try:
            db.commit()
            assert False, "Expected same-tenant duplicate identity to fail."
        except IntegrityError:
            db.rollback()


def test_cross_tenant_guardrails_and_stamping(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-guardrails.db", monkeypatch=monkeypatch)
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="b-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_b,
    )
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#FF2200")
    _seed_company(SessionLocal, tenant_id=tenant_b, name="Tenant B Co", primary_color="#2288FF")

    with SessionLocal() as db:
        customer_a = Customer(tenant_id=tenant_a, account_code="A-001", name="A Customer")
        customer_b = Customer(tenant_id=tenant_b, account_code="B-001", name="B Customer")
        db.add_all([customer_a, customer_b])
        db.commit()
        db.refresh(customer_a)
        db.refresh(customer_b)
        customer_b_id = int(customer_b.id)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        assert tenant_a_client.get(f"/customers/{customer_b_id}").status_code == 404

        csrf = _prime_csrf(tenant_a_client)
        response = tenant_a_client.post(
            "/customers/new",
            data={
                "account_code": "A-NEW",
                "name": "New Tenant A Customer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

    with SessionLocal() as db:
        created = db.execute(
            select(Customer).where(Customer.account_code == "A-NEW")
        ).scalars().first()
        assert created is not None
        assert int(created.tenant_id) == tenant_a


def test_missing_csrf_is_rejected_on_tenant_and_admin_hosts(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-csrf.db", monkeypatch=monkeypatch)
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#AA3300")
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        response = tenant_a_client.post(
            "/customers/new",
            data={
                "account_code": "A-CSRF",
                "name": "CSRF Check",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        response = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "New Tenant",
                "subdomain": "newtenant",
                "admin_email": "new-admin@example.com",
                "admin_password": "NewTenant123!",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403


def test_superadmin_enable_disable_actions_block_tenant_access(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-enable-disable.db", monkeypatch=monkeypatch
    )
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#AA8800")
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_a_client)
        upload_logo = tenant_a_client.post(
            "/admin/company",
            data={"name": "Tenant A Co", "logo_action": "upload", CSRF_FORM_FIELD: csrf},
            files={"company_logo_file": ("logo.png", BytesIO(b"\x89PNG\r\n\x1a\ntenant-a"), "image/png")},
            follow_redirects=False,
        )
        assert upload_logo.status_code in {302, 303}

    with SessionLocal() as db:
        company_a = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_a).limit(1)
        ).scalars().first()
        assert company_a is not None
        logo_path = str(company_a.company_logo_path or "")
        assert logo_path.startswith("/static/uploads/company/")

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        new_form = admin_client.get("/platform/tenants/new")
        assert new_form.status_code == 200
        assert "/t/&lt;subdomain&gt;/login" in new_form.text
        assert "Used for tenant subdomains and the `/t/&lt;subdomain&gt;` fallback path." in new_form.text
        csrf = _prime_csrf(admin_client)
        disable = admin_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert disable.status_code in {302, 303}

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        tickets = tenant_a_client.get("/tickets")
        login = tenant_a_client.get("/login")
        branding = tenant_a_client.get("/branding.css")
        logo = tenant_a_client.get(logo_path)
        health = tenant_a_client.get("/health")
        assert tickets.status_code == 403
        assert login.status_code == 403
        assert branding.status_code == 403
        assert logo.status_code == 403
        assert health.status_code == 403
        assert "Tenant disabled" in login.text
        assert "Tenant disabled" in branding.text
        assert "Tenant disabled" in logo.text
        assert "Tenant disabled" in health.text

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        enable = admin_client.post(
            f"/platform/tenants/{tenant_a}/enable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert enable.status_code in {302, 303}

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert tenant_a_client.get("/login").status_code == 200


def test_superadmin_tenant_actions_enforce_scope_and_write_audit(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-admin-actions.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#124578")
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)

        tenants_page = admin_client.get("/platform/tenants")
        assert tenants_page.status_code == 200
        assert "Tenant Management" in tenants_page.text
        assert "Tenant A" in tenants_page.text
        assert "tenant-admin@example.com" in tenants_page.text
        assert f"/platform/tenants/{tenant_a}" in tenants_page.text
        assert f"/platform/tenants/{tenant_a}/disable" in tenants_page.text
        assert f"/platform/tenants/{tenant_a}/delete" in tenants_page.text
        assert "/t/a/login" in tenants_page.text
        assert 'name="csrf_token"' in tenants_page.text

        tenant_detail = admin_client.get(f"/platform/tenants/{tenant_a}")
        assert tenant_detail.status_code == 200
        assert "Tenant A" in tenant_detail.text
        assert "Tenant details, access state, and user summary." in tenant_detail.text
        assert "tenant-admin@example.com" in tenant_detail.text
        assert 'href="/t/a/login"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/delete"' in tenant_detail.text

        disable = admin_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert disable.status_code in {302, 303}

        csrf = _prime_csrf(admin_client)
        enable = admin_client.post(
            f"/platform/tenants/{tenant_a}/enable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert enable.status_code in {302, 303}

    with SessionLocal() as db:
        disable_event = db.execute(
            select(AuditEvent)
            .where(AuditEvent.action == "TENANT_DISABLE", AuditEvent.entity_id == str(tenant_a))
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        enable_event = db.execute(
            select(AuditEvent)
            .where(AuditEvent.action == "TENANT_ENABLE", AuditEvent.entity_id == str(tenant_a))
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert disable_event is not None
        assert enable_event is not None
        assert disable_event.user_id == superadmin_id
        assert enable_event.user_id == superadmin_id
        assert disable_event.entity_type == "tenant"
        assert enable_event.entity_type == "tenant"
        assert disable_event.occurred_at is not None
        assert enable_event.occurred_at is not None
        assert isinstance(disable_event.details_json, dict)
        assert isinstance(enable_event.details_json, dict)
        assert disable_event.details_json.get("subdomain") == "a"
        assert enable_event.details_json.get("subdomain") == "a"

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="tenant-admin@example.com", password="TenantPass123!") == 303
        tenant_admin_page = tenant_client.get("/admin/company")
        assert tenant_admin_page.status_code == 200
        assert "Tenant Management" not in tenant_admin_page.text
        csrf = _prime_csrf(tenant_client)
        tenant_session_cookie = str(tenant_client.cookies.get("session") or "")
        assert tenant_session_cookie
        forbidden_tenant_host = tenant_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_tenant_host.status_code == 404

    with _client(app, base_url="https://admin.localhost") as admin_host_client:
        csrf = _prime_csrf(admin_host_client)
        admin_host_client.cookies.set("session", tenant_session_cookie)
        forbidden_non_superadmin = admin_host_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_non_superadmin.status_code in {302, 403}

    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_a)
        assert tenant is not None
        assert bool(tenant.is_active) is True


def test_superadmin_can_delete_empty_tenant_and_delete_blocks_linked_data(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-delete-flow.db", monkeypatch=monkeypatch
    )
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    empty_tenant = _seed_tenant(SessionLocal, name="Empty Tenant", subdomain="empty", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=empty_tenant,
        company_name="Empty Tenant",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="empty-admin@example.com",
        password="EmptyPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=empty_tenant,
    )
    (uploads_root / "tenants" / str(empty_tenant) / "company").mkdir(parents=True, exist_ok=True)
    (uploads_root / "tenants" / str(empty_tenant) / "company" / "logo.png").write_bytes(b"empty-logo")

    busy_tenant = _seed_tenant(SessionLocal, name="Busy Tenant", subdomain="busy", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=busy_tenant,
        company_name="Busy Tenant",
        primary_color="#335577",
    )
    _seed_user(
        SessionLocal,
        email="busy-admin@example.com",
        password="BusyPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=busy_tenant,
    )
    with SessionLocal() as db:
        db.add(Customer(tenant_id=busy_tenant, account_code="BUSY-001", name="Busy Customer"))
        db.commit()

    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

        tenants_page = admin_client.get("/platform/tenants")
        assert tenants_page.status_code == 200
        assert f"/platform/tenants/{empty_tenant}/delete" in tenants_page.text
        assert f"/platform/tenants/{busy_tenant}/delete" not in tenants_page.text

        busy_detail = admin_client.get(f"/platform/tenants/{busy_tenant}")
        assert busy_detail.status_code == 200
        assert "Delete is blocked because this tenant still has customer records." in busy_detail.text

        csrf = _prime_csrf(admin_client)
        blocked_delete = admin_client.post(
            f"/platform/tenants/{busy_tenant}/delete",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert blocked_delete.status_code in {302, 303}
        assert blocked_delete.headers.get("location", "").startswith(f"/platform/tenants/{busy_tenant}?")

        csrf = _prime_csrf(admin_client)
        delete_empty = admin_client.post(
            f"/platform/tenants/{empty_tenant}/delete",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert delete_empty.status_code in {302, 303}
        assert delete_empty.headers.get("location") == "/platform/tenants?deleted_tenant=empty"

        deleted_list = admin_client.get(delete_empty.headers["location"])
        assert deleted_list.status_code == 200
        assert "Tenant empty deleted." in deleted_list.text
        assert "Empty Tenant" not in deleted_list.text
        assert "Busy Tenant" in deleted_list.text

    with SessionLocal() as db:
        assert db.get(Tenant, empty_tenant) is None
        assert db.execute(select(User).where(User.tenant_id == empty_tenant)).scalars().first() is None
        assert (
            db.execute(select(CompanySetting).where(CompanySetting.tenant_id == empty_tenant))
            .scalars()
            .first()
            is None
        )
        assert db.get(Tenant, busy_tenant) is not None

    assert not (uploads_root / "tenants" / str(empty_tenant)).exists()


def test_non_superadmin_cannot_delete_tenant(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-delete-scope.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A",
        primary_color="#2a4768",
    )
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="tenant-admin@example.com", password="TenantPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        tenant_session_cookie = str(tenant_client.cookies.get("session") or "")
        assert tenant_session_cookie
        forbidden_tenant_host = tenant_client.post(
            f"/platform/tenants/{tenant_a}/delete",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_tenant_host.status_code == 404

    with _client(app, base_url="https://admin.localhost") as admin_host_client:
        csrf = _prime_csrf(admin_host_client)
        admin_host_client.cookies.set("session", tenant_session_cookie)
        forbidden_non_superadmin = admin_host_client.post(
            f"/platform/tenants/{tenant_a}/delete",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_non_superadmin.status_code in {302, 403}

    with SessionLocal() as db:
        assert db.get(Tenant, tenant_a) is not None


def test_platform_mode_limits_navigation_and_blocks_ticket_ui(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-platform-scope.db", monkeypatch=monkeypatch
    )
    _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

        home = admin_client.get("/", follow_redirects=False)
        assert home.status_code == 303
        assert home.headers.get("location") == "/platform/tenants"

        tenants_page = admin_client.get("/platform/tenants")
        assert tenants_page.status_code == 200
        assert ">Tenant Management<" in tenants_page.text
        assert ">System Status<" in tenants_page.text
        assert ">Tickets<" not in tenants_page.text
        assert ">Customers<" not in tenants_page.text
        assert ">Vehicles<" not in tenants_page.text
        assert ">Products<" not in tenants_page.text
        assert ">Invoices<" not in tenants_page.text
        assert ">Lookups<" not in tenants_page.text
        assert ">Reports<" not in tenants_page.text

        blocked_tickets = admin_client.get("/tickets")
        assert blocked_tickets.status_code == 404
        assert "Unknown tenant" in blocked_tickets.text


def test_superadmin_tenant_create_validates_reserved_and_normalizes_subdomain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reserved_subdomains", "admin,www,api,static,ops")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-subdomain-rules.db", monkeypatch=monkeypatch
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)

        missing_name = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "",
                "subdomain": "missing-name",
                "admin_email": "missing-name@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert missing_name.status_code == 400
        assert "Tenant name is required." in missing_name.text
        assert "tenant-form__error" in missing_name.text

        reserved = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Reserved Admin",
                "subdomain": "admin",
                "admin_email": "reserved-admin@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert reserved.status_code == 400
        assert "Subdomain is reserved." in reserved.text

        configured_reserved = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Reserved Ops",
                "subdomain": "ops",
                "admin_email": "reserved-ops@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert configured_reserved.status_code == 400
        assert "Subdomain is reserved." in configured_reserved.text

        invalid = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Invalid Label",
                "subdomain": "bad_label",
                "admin_email": "invalid-label@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert invalid.status_code == 400
        assert "Subdomain must be DNS-safe lowercase letters, numbers, and hyphens." in invalid.text

        created = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Normalized Tenant",
                "subdomain": "MyTenant",
                "admin_email": "normalized-admin@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert created.status_code in {302, 303}
        assert created.headers.get("location") == "/platform/tenants?created_tenant=mytenant"

        created_list = admin_client.get(created.headers["location"])
        assert created_list.status_code == 200
        assert "Tenant mytenant created." in created_list.text
        assert "Fallback sign-in is available at" in created_list.text
        assert 'href="/t/mytenant/login"' in created_list.text
        assert "https://mytenant.example.test/login" not in created_list.text
        assert "normalized-admin@example.com" in created_list.text

    with SessionLocal() as db:
        tenant = db.execute(select(Tenant).where(Tenant.name == "Normalized Tenant")).scalars().first()
        assert tenant is not None
        assert tenant.subdomain == "mytenant"


def test_path_based_tenant_access_mode_on_base_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-path-access.db", monkeypatch=monkeypatch
    )
    default_tenant = _seed_tenant(
        SessionLocal,
        name="Default Tenant",
        subdomain=settings.effective_default_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=default_tenant,
        company_name="Default Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="default-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=default_tenant,
    )

    tenant_mjteale = _seed_tenant(SessionLocal, name="MJ Teale", subdomain="mjteale", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_mjteale,
        company_name="MJ Teale",
        primary_color="#334455",
    )
    _seed_user(
        SessionLocal,
        email="mjteale-admin@example.com",
        password="PathPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_mjteale,
    )

    tenant_disabled = _seed_tenant(SessionLocal, name="Disabled Tenant", subdomain="disabled", is_active=False)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_disabled,
        company_name="Disabled Co",
        primary_color="#111111",
    )

    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://example.test") as tenant_client:
        login_page = tenant_client.get("/t/mjteale/login")
        assert login_page.status_code == 200
        assert 'action="/t/mjteale/login"' in login_page.text
        assert _login(
            tenant_client,
            email="mjteale-admin@example.com",
            password="PathPass123!",
            login_path="/t/mjteale/login",
        ) == 303

        tickets = tenant_client.get("/t/mjteale/tickets")
        assert tickets.status_code == 200
        assert "Tickets" in tickets.text
        assert 'href="/t/mjteale/customers"' in tickets.text

        admin_company = tenant_client.get("/t/mjteale/admin/company")
        assert admin_company.status_code == 200

        disabled_login = tenant_client.get("/t/disabled/login")
        assert disabled_login.status_code == 403
        assert "Tenant disabled" in disabled_login.text

    with _client(app, base_url="https://admin.example.test") as platform_client:
        assert (
            _login(
                platform_client,
                email="superadmin@example.com",
                password="TestPass123!",
                next_path="/platform/tenants",
            )
            == 303
        )
        new_form = platform_client.get("/platform/tenants/new")
        assert new_form.status_code == 200
        assert "https://&lt;subdomain&gt;.example.test/login" in new_form.text
        assert "/t/&lt;subdomain&gt;/login" not in new_form.text
        assert "Used for the tenant subdomain in production sign-in URLs." in new_form.text

        platform_page = platform_client.get("/platform/tenants")
        assert platform_page.status_code == 200
        assert 'href="https://mjteale.example.test/login"' in platform_page.text
        assert "/t/mjteale/login" not in platform_page.text

        tenant_detail = platform_client.get(f"/platform/tenants/{tenant_mjteale}")
        assert tenant_detail.status_code == 200
        assert 'href="https://mjteale.example.test/login"' in tenant_detail.text
        assert "/t/mjteale/login" not in tenant_detail.text

        csrf = _prime_csrf(platform_client)
        created = platform_client.post(
            "/platform/tenants/new",
            data={
                "name": "Custom Domain Tenant",
                "subdomain": "customdomain",
                "admin_email": "customdomain-admin@example.com",
                "admin_password": "CustomDomain123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert created.status_code in {302, 303}
        assert created.headers.get("location") == "/platform/tenants?created_tenant=customdomain"

        created_list = platform_client.get(created.headers["location"])
        assert created_list.status_code == 200
        assert "Tenant customdomain created." in created_list.text
        assert "Sign in at" in created_list.text
        assert 'href="https://customdomain.example.test/login"' in created_list.text
        assert "/t/customdomain/login" not in created_list.text

    with _client(app, base_url=f"https://{settings.effective_default_tenant_subdomain}.example.test") as default_client:
        assert (
            _login(
                default_client,
                email="default-admin@example.com",
                password="TenantPass123!",
            )
            == 303
        )
        assert default_client.get("/tickets").status_code == 200


def test_forwarded_host_trust_flag_controls_tenant_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "proxy.localhost,a.localhost")
    monkeypatch.setattr(settings, "trust_forwarded_host", False)
    app_untrusted, SessionUntrusted = _build_app_and_session(
        tmp_path, db_name="tenant-forwarded-untrusted.db", monkeypatch=monkeypatch
    )
    _seed_tenant(SessionUntrusted, name="Tenant A", subdomain="a", is_active=True)

    with _client(app_untrusted, base_url="https://proxy.localhost") as client:
        response = client.get("/health", headers={"x-forwarded-host": "a.localhost"})
        assert response.status_code == 404
        assert "Unknown tenant" in response.text

    monkeypatch.setattr(settings, "trust_forwarded_host", True)
    app_trusted, SessionTrusted = _build_app_and_session(
        tmp_path, db_name="tenant-forwarded-trusted.db", monkeypatch=monkeypatch
    )
    _seed_tenant(SessionTrusted, name="Tenant A", subdomain="a", is_active=True)

    with _client(app_trusted, base_url="https://proxy.localhost") as client:
        response = client.get("/health", headers={"x-forwarded-host": "a.localhost"})
        assert response.status_code == 200


def test_allowed_hosts_enforced_with_resolved_host_value(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "a.localhost")
    monkeypatch.setattr(settings, "trust_forwarded_host", True)
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-allowed-hosts.db", monkeypatch=monkeypatch
    )
    _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)

    with _client(app, base_url="https://proxy.localhost") as client:
        allowed = client.get("/health", headers={"x-forwarded-host": "a.localhost"})
        assert allowed.status_code == 200

        blocked = client.get("/health", headers={"x-forwarded-host": "b.localhost"})
        assert blocked.status_code == 404


def test_multi_tenant_smoke(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-smoke.db", monkeypatch=monkeypatch)
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="b-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_b,
    )
    _seed_company(SessionLocal, tenant_id=tenant_a, name="Tenant A Co", primary_color="#111111")
    _seed_company(SessionLocal, tenant_id=tenant_b, name="Tenant B Co", primary_color="#0055CC")

    with SessionLocal() as db:
        seed_required_reference_data(db)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_a_client)
        create_customer = tenant_a_client.post(
            "/customers/new",
            data={
                "account_code": "A-SMOKE",
                "name": "Tenant A Smoke Customer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_customer.status_code in {302, 303}

        quick_ticket = tenant_a_client.post(
            "/tickets/new/quick",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert quick_ticket.status_code in {302, 303}

    with SessionLocal() as db:
        db.add(
            Product(
                tenant_id=tenant_a,
                code="A-PROD",
                description="Tenant A Product",
                unit_price=1,
            )
        )
        db.commit()

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="b-admin@example.com", password="TestPass123!") == 303
        customers_html = tenant_b_client.get("/customers").text
        products_html = tenant_b_client.get("/products").text
        tickets_html = tenant_b_client.get("/tickets").text
        assert "A-SMOKE" not in customers_html
        assert "A-PROD" not in products_html
        assert "Tenant A Smoke Customer" not in customers_html
        assert "A-SMOKE" not in tickets_html
        assert "A-PROD" not in tickets_html

        branding_b = tenant_b_client.get("/branding.css").text

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        branding_a = tenant_a_client.get("/branding.css").text
    assert branding_a != branding_b
    assert "#111111" in branding_a
    assert "#0055CC" in branding_b

    with SessionLocal() as db:
        tenant_b_obj = db.get(Tenant, tenant_b)
        assert tenant_b_obj is not None
        tenant_b_obj.is_active = False
        db.commit()

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert tenant_b_client.get("/health").status_code == 403


def test_new_tenant_creation_flow_seeds_usable_baseline(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-create-seed.db", monkeypatch=monkeypatch
    )
    _ = SessionLocal
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        create_tenant = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Tenant Seeded",
                "subdomain": "seeded",
                "admin_email": "seeded-admin@example.com",
                "admin_password": "SeededPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_tenant.status_code in {302, 303}

    with _client(app, base_url="https://seeded.localhost") as tenant_client:
        assert _login(tenant_client, email="seeded-admin@example.com", password="SeededPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        create_product = tenant_client.post(
            "/products/new",
            data={
                "code": "SEED-PROD-1",
                "description": "Seeded Product",
                "sale_type": "WEIGHT",
                "unit_price": "12.50",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_product.status_code in {302, 303}

        create_customer = tenant_client.post(
            "/customers/new",
            data={
                "account_code": "SEED-CUST-1",
                "name": "Seeded Customer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_customer.status_code in {302, 303}

        quick_ticket = tenant_client.post(
            "/tickets/new/quick",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert quick_ticket.status_code in {302, 303}
        ticket_location = str(quick_ticket.headers.get("location", ""))
        assert ticket_location.startswith("/tickets/")
        ticket_id = int(ticket_location.split("?", 1)[0].rstrip("/").split("/")[-1])

        with SessionLocal() as db:
            customer = db.execute(
                select(Customer).where(Customer.account_code == "SEED-CUST-1").limit(1)
            ).scalars().first()
            product = db.execute(
                select(Product).where(Product.code == "SEED-PROD-1").limit(1)
            ).scalars().first()
            assert customer is not None
            assert product is not None
            customer_id = int(customer.id)
            product_id = int(product.id)

        complete_ticket = tenant_client.post(
            f"/tickets/{ticket_id}",
            data={
                "action": "complete",
                "datetime": "2026-02-11T10:20",
                "direction": "INWARD",
                "transaction_type": "SALE",
                "customer_id": str(customer_id),
                "product_id": str(product_id),
                "gross_kg": "2500",
                "tare_kg": "1000",
                "unit_price": "12.50",
                "po_number": "",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert complete_ticket.status_code in {302, 303}
        assert complete_ticket.headers.get("location", "").endswith(f"/tickets/{ticket_id}?completed=1")

        with SessionLocal() as db:
            completed_ticket = db.get(Ticket, ticket_id)
            assert completed_ticket is not None
            completed_status = getattr(completed_ticket.status, "value", completed_ticket.status)
            assert str(completed_status).upper() == "COMPLETE"
            ticket_no = str(completed_ticket.ticket_no or "")
            assert ticket_no

        invoice_candidates_form = tenant_client.get("/invoices/generate")
        assert invoice_candidates_form.status_code == 200
        invoice_candidates = tenant_client.post(
            "/invoices/generate",
            data={
                "customer_id": str(customer_id),
                "date_from": "01/02/2026",
                "date_to": "28/02/2026",
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert invoice_candidates.status_code == 200
        assert ticket_no in invoice_candidates.text


def test_tenant_scoped_logo_upload_and_file_access_isolation(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-logos.db", monkeypatch=monkeypatch)
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#111111",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#0055CC",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="b-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_b,
    )

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_a_client)
        upload_a = tenant_a_client.post(
            "/admin/company",
            data={"name": "Tenant A Co", "logo_action": "upload", CSRF_FORM_FIELD: csrf},
            files={"company_logo_file": ("logo.png", BytesIO(b"\x89PNG\r\n\x1a\na-logo"), "image/png")},
            follow_redirects=False,
        )
        assert upload_a.status_code in {302, 303}

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="b-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_b_client)
        upload_b = tenant_b_client.post(
            "/admin/company",
            data={"name": "Tenant B Co", "logo_action": "upload", CSRF_FORM_FIELD: csrf},
            files={"company_logo_file": ("logo.png", BytesIO(b"\x89PNG\r\n\x1a\nb-logo"), "image/png")},
            follow_redirects=False,
        )
        assert upload_b.status_code in {302, 303}

    with SessionLocal() as db:
        company_a = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_a).limit(1)
        ).scalars().first()
        company_b = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_b).limit(1)
        ).scalars().first()
        assert company_a is not None and company_b is not None
        logo_a = str(company_a.company_logo_path or "")
        logo_b = str(company_b.company_logo_path or "")
        assert logo_a.startswith("/static/uploads/company/")
        assert logo_b.startswith("/static/uploads/company/")
        assert logo_a != logo_b

    filename_a = Path(logo_a).name
    filename_b = Path(logo_b).name
    file_a = uploads_root / "tenants" / str(tenant_a) / "company" / filename_a
    file_b = uploads_root / "tenants" / str(tenant_b) / "company" / filename_b
    assert file_a.is_file()
    assert file_b.is_file()
    assert file_a.read_bytes() != file_b.read_bytes()
    assert not (uploads_root / "company" / filename_a).exists()
    assert not (uploads_root / "company" / filename_b).exists()
    shared_static_company = (Path(__file__).resolve().parents[1] / "app" / "static" / "uploads" / "company")
    assert not (shared_static_company / filename_a).exists()
    assert not (shared_static_company / filename_b).exists()

    collision_name = "logo-collision.png"
    collision_url = f"/static/uploads/company/{collision_name}"
    collision_a = uploads_root / "tenants" / str(tenant_a) / "company" / collision_name
    collision_b = uploads_root / "tenants" / str(tenant_b) / "company" / collision_name
    collision_a.write_bytes(b"\x89PNG\r\n\x1a\ncollision-a")
    collision_b.write_bytes(b"\x89PNG\r\n\x1a\ncollision-b")

    with SessionLocal() as db:
        company_a = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_a).limit(1)
        ).scalars().first()
        company_b = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_b).limit(1)
        ).scalars().first()
        assert company_a is not None and company_b is not None
        company_a.company_logo_path = collision_url
        company_b.company_logo_path = collision_url
        db.commit()

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        css_a = tenant_a_client.get("/branding.css")
        assert css_a.status_code == 200
        assert collision_name in css_a.text
        assert filename_b not in css_a.text
        own_collision = tenant_a_client.get(collision_url)
        other_logo = tenant_a_client.get(f"/static/uploads/company/{filename_b}")
        assert own_collision.status_code == 200
        assert own_collision.content == b"\x89PNG\r\n\x1a\ncollision-a"
        assert other_logo.status_code == 404
        assert (
            tenant_a_client.get(f"/static/uploads/company/%2e%2e%2f{filename_b}").status_code
            == 404
        )
        assert (
            tenant_a_client.get(f"/static/uploads/company/%2e%2e%5c{filename_b}").status_code
            == 404
        )

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        css_b = tenant_b_client.get("/branding.css")
        assert css_b.status_code == 200
        assert collision_name in css_b.text
        assert filename_a not in css_b.text
        own_collision = tenant_b_client.get(collision_url)
        assert own_collision.status_code == 200
        assert own_collision.content == b"\x89PNG\r\n\x1a\ncollision-b"


def test_global_ewc_reads_on_tenant_host_and_admin_management_is_platform_only(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(tmp_path, db_name="tenant-ewc-policy.db", monkeypatch=monkeypatch)
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#223344",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with SessionLocal() as db:
        db.add(
            EwcCode(
                code_6="170904",
                code_display="17 09 04",
                description="Mixed construction waste",
                hazardous=False,
                active=True,
                source_file="seed.csv",
                imported_at=utcnow(),
            )
        )
        db.commit()

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        products_new = tenant_client.get("/products/new")
        assert products_new.status_code == 200
        assert "17 09 04" in products_new.text
        assert tenant_client.get("/admin/ewc-codes").status_code == 404

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        assert admin_client.get("/admin/ewc-codes").status_code == 200
