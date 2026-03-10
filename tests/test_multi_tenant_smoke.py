from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
import threading
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_USER, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import (
    AuditEvent,
    Area,
    Base,
    CompanySetting,
    Customer,
    DirectionEnum,
    EwcCode,
    Haulier,
    Invoice,
    PrintDestination,
    PrintTemplate,
    Product,
    Tenant,
    Ticket,
    TicketSequence,
    TicketStatusEnum,
    TransactionTypeEnum,
    Unit,
    User,
    Vehicle,
    VehicleType,
    Yard,
)
from app.models.base import utcnow
from app.routes.tickets import _generate_ticket_no
from app.seed import seed_print_destinations, seed_print_templates
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from app.services.print_context import build_print_base_context
from app.services.print_payload import _company_logo_src
from app.services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from app.templating import templates
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


def _extract_nav_markup(html: str) -> str:
    match = re.search(r'<nav class="site-nav">(.*?)</nav>', html, flags=re.DOTALL)
    assert match is not None
    return match.group(1)


def _extract_utility_bar_markup(html: str) -> str:
    match = re.search(
        r'<div class="site-utility-bar">(.*?)</div>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def _prime_csrf(client: TestClient, *, login_path: str = "/login") -> str:
    response = client.get(login_path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    return token


def _dashboard_metric_value(html: str, key: str) -> str:
    pattern = (
        rf'data-dashboard-metric="{re.escape(key)}".*?'
        r'<div class="dashboard-stat-card__value">(.*?)</div>'
    )
    match = re.search(pattern, html, flags=re.DOTALL)
    assert match is not None
    return re.sub(r"\s+", " ", match.group(1)).strip()


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
    is_demo: bool = False,
) -> int:
    with SessionLocal() as db:
        tenant = Tenant(
            name=name,
            subdomain=subdomain,
            is_active=is_active,
            is_demo=is_demo,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _create_ticket_with_generated_number(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    when: datetime,
    barrier: threading.Barrier | None = None,
) -> str:
    with SessionLocal() as db:
        db.info["tenant_id"] = int(tenant_id)
        db.info["platform_mode"] = False
        if barrier is not None:
            barrier.wait(timeout=5)
        ticket_no = _generate_ticket_no(db, now=when)
        db.add(
            Ticket(
                tenant_id=int(tenant_id),
                ticket_no=ticket_no,
                datetime=when,
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()
        return str(ticket_no)


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


def test_software_subdomain_serves_marketing_page_and_other_subdomains_still_route(
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

    with _client(app, base_url="https://example.test") as apex_client:
        landing_redirect = apex_client.get("/", follow_redirects=False)
        assert landing_redirect.status_code == 307
        assert landing_redirect.headers.get("location") == "https://software.example.test/"

    with _client(app, base_url="https://software.example.test") as marketing_client:
        landing = marketing_client.get("/")
        assert landing.status_code == 200
        assert "Weighbridge Web" in landing.text
        assert "Cloud software for weighbridge operations." in landing.text
        assert "Built for day-to-day weighbridge work" in landing.text
        assert "Cloud software for ticketing, compliance, and invoicing." in landing.text
        assert 'name="description"' in landing.text
        assert 'property="og:title"' in landing.text
        assert 'property="og:description"' in landing.text
        assert "/static/css/marketing.css" in landing.text
        assert "https://software.example.test/" in landing.text

        blocked_tickets = marketing_client.get("/tickets")
        assert blocked_tickets.status_code == 404
        blocked_platform = marketing_client.get("/platform/tenants")
        assert blocked_platform.status_code == 404
        blocked_login = marketing_client.get("/login")
        assert blocked_login.status_code == 404

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


def test_ticket_numbers_are_scoped_per_tenant_year_and_concurrency_safe(
    tmp_path,
    monkeypatch,
):
    _app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ticket-numbering.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="tenant-a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="tenant-b")

    tenant_a_numbers = [
        _create_ticket_with_generated_number(
            SessionLocal,
            tenant_id=tenant_a,
            when=datetime(2026, 3, 8, 9, minute_offset, 0),
        )
        for minute_offset in range(3)
    ]
    tenant_b_numbers = [
        _create_ticket_with_generated_number(
            SessionLocal,
            tenant_id=tenant_b,
            when=datetime(2026, 3, 8, 10, minute_offset, 0),
        )
        for minute_offset in range(2)
    ]
    tenant_a_next_year = _create_ticket_with_generated_number(
        SessionLocal,
        tenant_id=tenant_a,
        when=datetime(2027, 1, 2, 8, 0, 0),
    )

    assert tenant_a_numbers == ["26-00001", "26-00002", "26-00003"]
    assert tenant_b_numbers == ["26-00001", "26-00002"]
    assert tenant_a_next_year == "27-00001"

    barrier = threading.Barrier(4)
    concurrent_when = datetime(2026, 3, 8, 11, 0, 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                _create_ticket_with_generated_number,
                SessionLocal,
                tenant_id=tenant_b,
                when=concurrent_when + timedelta(minutes=index),
                barrier=barrier,
            )
            for index in range(4)
        ]
    concurrent_numbers = sorted(future.result() for future in futures)

    assert concurrent_numbers == ["26-00003", "26-00004", "26-00005", "26-00006"]

    with SessionLocal() as db:
        tenant_a_tickets = list(
            db.execute(
                select(Ticket.ticket_no)
                .where(Ticket.tenant_id == tenant_a)
                .order_by(Ticket.datetime.asc(), Ticket.id.asc())
            ).scalars()
        )
        tenant_b_tickets = list(
            db.execute(
                select(Ticket.ticket_no)
                .where(Ticket.tenant_id == tenant_b)
                .order_by(Ticket.datetime.asc(), Ticket.id.asc())
            ).scalars()
        )

    assert tenant_a_tickets == ["26-00001", "26-00002", "26-00003", "27-00001"]
    assert sorted(tenant_b_tickets) == [
        "26-00001",
        "26-00002",
        "26-00003",
        "26-00004",
        "26-00005",
        "26-00006",
    ]


def test_ticket_number_uniqueness_is_tenant_scoped_but_still_enforced_within_tenant(
    tmp_path,
    monkeypatch,
):
    _app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ticket-unique.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="tenant-a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="tenant-b")
    ticket_when = datetime(2026, 3, 8, 9, 0, 0)

    with SessionLocal() as db:
        db.add(
            Ticket(
                tenant_id=tenant_a,
                ticket_no="26-00001",
                datetime=ticket_when,
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

        db.add(
            Ticket(
                tenant_id=tenant_b,
                ticket_no="26-00001",
                datetime=ticket_when,
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

        db.add(
            Ticket(
                tenant_id=tenant_a,
                ticket_no="26-00001",
                datetime=ticket_when + timedelta(minutes=5),
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                dont_invoice=False,
                paid=False,
            )
        )
        try:
            db.commit()
            assert False, "Expected same-tenant duplicate ticket_no to fail."
        except IntegrityError:
            db.rollback()


def test_platform_bootstrap_on_admin_subdomain_creates_first_superadmin_without_breaking_tenant_access(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-platform-bootstrap.db", monkeypatch=monkeypatch
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
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
        demo_admin = (
            db.execute(
                select(User).where(getattr(User, "email", getattr(User, "username")) == "demo-admin@example.com")
            )
            .scalars()
            .first()
        )
        assert platform_owner is not None
        assert platform_owner.tenant_id is None
        assert str(platform_owner.role or "").strip().lower() == ROLE_SUPERADMIN
        assert demo_admin is not None
        assert int(demo_admin.tenant_id or 0) == demo_tenant

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.example.test") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="demo-admin@example.com",
                password="TenantPass123!",
            )
            == 303
        )
        assert tenant_client.get("/admin/company").status_code == 200
        assert tenant_client.get("/tickets").status_code == 200


def test_tenant_login_points_first_run_to_platform_bootstrap_host_when_needed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-login-bootstrap-link.db",
        monkeypatch=monkeypatch,
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#225577",
    )

    with _client(
        app,
        base_url=f"https://{settings.effective_demo_tenant_subdomain}.example.test",
    ) as tenant_client:
        login_page = tenant_client.get("/login")
        assert login_page.status_code == 200
        assert "Create the first platform administrator" in login_page.text
        assert 'href="https://admin.example.test/platform/bootstrap"' in login_page.text


def test_tenant_login_asks_platform_admin_when_workspace_has_no_users_but_platform_is_bootstrapped(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-login-platform-owner.db",
        monkeypatch=monkeypatch,
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="platform-owner@example.com",
        password="PlatformPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(
        app,
        base_url=f"https://{settings.effective_demo_tenant_subdomain}.example.test",
    ) as tenant_client:
        login_page = tenant_client.get("/login")
        assert login_page.status_code == 200
        assert (
            "Ask a platform administrator to create the first workspace admin."
            in login_page.text
        )
        assert "/platform/bootstrap" not in login_page.text


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


def test_tenant_admin_can_update_their_sign_in_email_from_company_settings(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-company-email.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#225577",
    )
    user_a = _seed_user(
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

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        settings_page = tenant_client.get("/admin/company")
        assert settings_page.status_code == 200
        assert 'name="login_email"' in settings_page.text
        assert 'value="a-admin@example.com"' in settings_page.text

        csrf = _prime_csrf(tenant_client)
        update_response = tenant_client.post(
            "/admin/company",
            data={
                "name": "Tenant A Co",
                "login_email": "ops-admin@example.com",
                "navbar_color_hex": "#113355",
                "primary_color_hex": "#225577",
                "nav_logo_height_px": "34",
                "show_nav_logo": "1",
                "show_nav_title": "1",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert update_response.status_code == 303
        assert update_response.headers.get("location") == "/admin/company?saved=1&account_saved=1"

    with SessionLocal() as db:
        updated_user_a = db.get(User, user_a)
        user_b = (
            db.execute(select(User).where(User.tenant_id == tenant_b).limit(1))
            .scalars()
            .first()
        )
        assert updated_user_a is not None
        assert user_b is not None
        assert str(updated_user_a.username or "") == "ops-admin@example.com"
        assert str(user_b.username or "") == "b-admin@example.com"

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 401
        assert _login(tenant_client, email="ops-admin@example.com", password="TestPass123!") == 303
        refreshed_settings_page = tenant_client.get("/admin/company?saved=1&account_saved=1")
        assert refreshed_settings_page.status_code == 200
        assert "Company settings and sign-in email saved." in refreshed_settings_page.text
        assert 'value="ops-admin@example.com"' in refreshed_settings_page.text

    with _client(app, base_url="https://b.localhost") as tenant_client:
        assert _login(tenant_client, email="b-admin@example.com", password="TestPass123!") == 303


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


def test_customer_account_codes_and_vehicle_registrations_are_tenant_scoped(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-lookup-unique.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#AA2200",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#0044AA",
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

    with SessionLocal() as db:
        vehicle_type = db.execute(select(VehicleType).limit(1)).scalars().first()
        assert vehicle_type is not None
        vehicle_type_id = int(vehicle_type.id)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(
            tenant_a_client,
            email="a-admin@example.com",
            password="TestPass123!",
        ) == 303
        csrf = _prime_csrf(tenant_a_client)
        create_customer = tenant_a_client.post(
            "/customers/new",
            data={
                "account_code": "SHARED-001",
                "name": "Tenant A Shared Customer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_customer.status_code in {302, 303}
        create_vehicle = tenant_a_client.post(
            "/vehicles/new",
            data={
                "registration": "SHARED-VEH",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_vehicle.status_code in {302, 303}
        duplicate_customer = tenant_a_client.post(
            "/customers/new",
            data={
                "account_code": "SHARED-001",
                "name": "Tenant A Duplicate Customer",
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert duplicate_customer.status_code == 400
        assert "Account code already exists." in duplicate_customer.text
        duplicate_vehicle = tenant_a_client.post(
            "/vehicles/new",
            data={
                "registration": "SHARED-VEH",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert duplicate_vehicle.status_code == 400
        assert "Registration already exists." in duplicate_vehicle.text

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(
            tenant_b_client,
            email="b-admin@example.com",
            password="TestPass123!",
        ) == 303
        csrf = _prime_csrf(tenant_b_client)
        create_customer = tenant_b_client.post(
            "/customers/new",
            data={
                "account_code": "SHARED-001",
                "name": "Tenant B Shared Customer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_customer.status_code in {302, 303}
        create_vehicle = tenant_b_client.post(
            "/vehicles/new",
            data={
                "registration": "SHARED-VEH",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_vehicle.status_code in {302, 303}

    with SessionLocal() as db:
        shared_customers = list(
            db.execute(
                select(Customer)
                .where(Customer.account_code == "SHARED-001")
                .order_by(Customer.tenant_id.asc(), Customer.id.asc())
            ).scalars()
        )
        shared_vehicles = list(
            db.execute(
                select(Vehicle)
                .where(Vehicle.registration == "SHAREDVEH")
                .order_by(Vehicle.tenant_id.asc(), Vehicle.id.asc())
            ).scalars()
        )
        assert [int(row.tenant_id) for row in shared_customers] == [tenant_a, tenant_b]
        assert [int(row.tenant_id) for row in shared_vehicles] == [tenant_a, tenant_b]


def test_customer_and_vehicle_uniqueness_updates_remain_tenant_scoped(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-lookup-update-unique.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#AA2200",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#0044AA",
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

    with SessionLocal() as db:
        vehicle_type = db.execute(select(VehicleType).limit(1)).scalars().first()
        assert vehicle_type is not None
        vehicle_type_id = int(vehicle_type.id)

        customer_a = Customer(
            tenant_id=tenant_a,
            account_code="ABC001",
            name="Tenant A Primary",
        )
        customer_a_other = Customer(
            tenant_id=tenant_a,
            account_code="ABC002",
            name="Tenant A Secondary",
        )
        customer_b = Customer(
            tenant_id=tenant_b,
            account_code="ABC001",
            name="Tenant B Primary",
        )
        vehicle_a = Vehicle(
            tenant_id=tenant_a,
            registration="AB12CDE",
            vehicle_type_id=vehicle_type_id,
        )
        vehicle_a_other = Vehicle(
            tenant_id=tenant_a,
            registration="ZZ99ZZZ",
            vehicle_type_id=vehicle_type_id,
        )
        vehicle_b = Vehicle(
            tenant_id=tenant_b,
            registration="AB12CDE",
            vehicle_type_id=vehicle_type_id,
        )
        db.add_all(
            [
                customer_a,
                customer_a_other,
                customer_b,
                vehicle_a,
                vehicle_a_other,
                vehicle_b,
            ]
        )
        db.commit()
        customer_a_id = int(customer_a.id)
        customer_a_other_id = int(customer_a_other.id)
        customer_b_id = int(customer_b.id)
        vehicle_a_id = int(vehicle_a.id)
        vehicle_a_other_id = int(vehicle_a_other.id)
        vehicle_b_id = int(vehicle_b.id)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(
            tenant_a_client,
            email="a-admin@example.com",
            password="TestPass123!",
        ) == 303
        csrf = _prime_csrf(tenant_a_client)

        customer_self_update = tenant_a_client.post(
            f"/customers/{customer_a_id}",
            data={
                "account_code": " abc001 ",
                "name": "Tenant A Updated",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert customer_self_update.status_code in {302, 303}

        customer_duplicate_update = tenant_a_client.post(
            f"/customers/{customer_a_other_id}",
            data={
                "account_code": "ABC001",
                "name": "Tenant A Duplicate",
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert customer_duplicate_update.status_code == 400
        assert "Account code already exists." in customer_duplicate_update.text

        vehicle_self_update = tenant_a_client.post(
            f"/vehicles/{vehicle_a_id}",
            data={
                "registration": " ab12 cde ",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert vehicle_self_update.status_code in {302, 303}

        vehicle_duplicate_update = tenant_a_client.post(
            f"/vehicles/{vehicle_a_other_id}",
            data={
                "registration": "AB12 CDE",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert vehicle_duplicate_update.status_code == 400
        assert "Registration already exists." in vehicle_duplicate_update.text

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(
            tenant_b_client,
            email="b-admin@example.com",
            password="TestPass123!",
        ) == 303
        csrf = _prime_csrf(tenant_b_client)

        customer_self_update = tenant_b_client.post(
            f"/customers/{customer_b_id}",
            data={
                "account_code": "ABC001",
                "name": "Tenant B Updated",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert customer_self_update.status_code in {302, 303}

        vehicle_self_update = tenant_b_client.post(
            f"/vehicles/{vehicle_b_id}",
            data={
                "registration": "AB12-CDE",
                "vehicle_type_id": str(vehicle_type_id),
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert vehicle_self_update.status_code in {302, 303}

    with SessionLocal() as db:
        refreshed_customer_a = db.get(Customer, customer_a_id)
        refreshed_customer_a_other = db.get(Customer, customer_a_other_id)
        refreshed_customer_b = db.get(Customer, customer_b_id)
        refreshed_vehicle_a = db.get(Vehicle, vehicle_a_id)
        refreshed_vehicle_a_other = db.get(Vehicle, vehicle_a_other_id)
        refreshed_vehicle_b = db.get(Vehicle, vehicle_b_id)

        assert refreshed_customer_a is not None
        assert refreshed_customer_a_other is not None
        assert refreshed_customer_b is not None
        assert refreshed_vehicle_a is not None
        assert refreshed_vehicle_a_other is not None
        assert refreshed_vehicle_b is not None

        assert refreshed_customer_a.account_code == "ABC001"
        assert refreshed_customer_a.name == "Tenant A Updated"
        assert refreshed_customer_a_other.account_code == "ABC002"
        assert refreshed_customer_b.account_code == "ABC001"
        assert refreshed_customer_b.name == "Tenant B Updated"
        assert refreshed_vehicle_a.registration == "AB12CDE"
        assert refreshed_vehicle_a_other.registration == "ZZ99ZZZ"
        assert refreshed_vehicle_b.registration == "AB12CDE"


def test_lookup_routes_and_ticket_reference_writes_are_tenant_isolated(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-lookup-isolation.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#992200",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#003399",
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

    with SessionLocal() as db:
        customer_b = Customer(
            account_code="B-CUST-1",
            name="Tenant B Customer",
            tenant_id=tenant_b,
        )
        vehicle_b = Vehicle(
            tenant_id=tenant_b,
            registration="B-ONLY-VEH",
        )
        haulier_b = Haulier(
            tenant_id=tenant_b,
            name="Tenant B Haulier",
            is_active=True,
        )
        area_b = Area(
            tenant_id=tenant_b,
            code="AREA-B",
            description="Tenant B Area",
            is_active=True,
        )
        db.add_all([customer_b, vehicle_b, haulier_b, area_b])
        db.flush()

        yard_b = db.execute(
            select(Yard).where(Yard.tenant_id == tenant_b).limit(1)
        ).scalars().first()
        assert yard_b is not None

        ticket_a = Ticket(
            tenant_id=tenant_a,
            ticket_no="A-GUARD-1",
            datetime=utcnow(),
            status=TicketStatusEnum.OPEN.value,
            direction=DirectionEnum.INWARD.value,
            transaction_type=TransactionTypeEnum.WASTEIN.value,
            dont_invoice=False,
            paid=False,
        )
        db.add(ticket_a)
        db.commit()
        db.refresh(customer_b)
        db.refresh(vehicle_b)
        db.refresh(haulier_b)
        db.refresh(area_b)
        db.refresh(ticket_a)
        customer_b_id = int(customer_b.id)
        vehicle_b_id = int(vehicle_b.id)
        haulier_b_id = int(haulier_b.id)
        area_b_id = int(area_b.id)
        yard_b_id = int(yard_b.id)
        ticket_a_id = int(ticket_a.id)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(
            tenant_a_client,
            email="a-admin@example.com",
            password="TestPass123!",
        ) == 303
        lookup_list = tenant_a_client.get("/lookups/hauliers")
        assert lookup_list.status_code == 200
        assert "Tenant B Haulier" not in lookup_list.text

        lookup_search = tenant_a_client.get("/lookups/hauliers?q=Tenant+B")
        assert lookup_search.status_code == 200
        assert "Tenant B Haulier" not in lookup_search.text

        lookup_edit = tenant_a_client.get(f"/lookups/hauliers/{haulier_b_id}/edit")
        assert lookup_edit.status_code == 404

        csrf = _prime_csrf(tenant_a_client)
        lookup_deactivate = tenant_a_client.post(
            f"/lookups/hauliers/{haulier_b_id}/deactivate",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert lookup_deactivate.status_code == 404

        vehicle_suggest = tenant_a_client.get(
            f"/tickets/vehicle-suggest?ticket_id={ticket_a_id}&reg=B-ONLY-VEH"
        )
        assert vehicle_suggest.status_code == 204

        ticket_update = tenant_a_client.post(
            f"/tickets/{ticket_a_id}",
            data={
                "action": "save",
                "datetime": "2026-03-08T10:30",
                "status": "OPEN",
                "direction": "INWARD",
                "transaction_type": "WASTEIN",
                "customer_id": str(customer_b_id),
                "vehicle_id": str(vehicle_b_id),
                "yard_id": str(yard_b_id),
                "area_id": str(area_b_id),
                "po_number": "",
                CSRF_FORM_FIELD: csrf,
            },
        )
        assert ticket_update.status_code == 400
        assert "Customer not found." in ticket_update.text
        assert "Vehicle not found." in ticket_update.text
        assert "Yard not found." in ticket_update.text
        assert "Area not found." in ticket_update.text

    with SessionLocal() as db:
        ticket_a = db.get(Ticket, ticket_a_id)
        assert ticket_a is not None
        assert ticket_a.customer_id is None
        assert ticket_a.vehicle_id is None
        assert ticket_a.yard_id is None
        assert ticket_a.area_id is None


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
        assert 'id="platform-tenants-title-help"' in tenants_page.text
        assert 'id="platform-total-tenants-help"' in tenants_page.text
        assert 'id="platform-initial-admin-help"' in tenants_page.text
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
        assert 'id="platform-tenant-detail-initial-admin-help"' in tenant_detail.text
        assert 'id="platform-tenant-users-help"' in tenant_detail.text
        assert "tenant-admin@example.com" in tenant_detail.text
        assert 'href="/t/a/login"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/users"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/admin-email"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/admin-password"' in tenant_detail.text
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
        tenant_nav = _extract_nav_markup(tenant_admin_page.text)
        tenant_utility = _extract_utility_bar_markup(tenant_admin_page.text)
        assert "Tenant Management" not in tenant_admin_page.text
        assert "Tenant A" not in tenant_nav
        assert "Signed in as tenant-admin@example.com" not in tenant_nav
        assert "Logout" not in tenant_nav
        assert "LOCALHOST" in tenant_admin_page.text
        assert "Tenant A" in tenant_utility
        assert "Signed in as tenant-admin@example.com" in tenant_utility
        assert "Logout" in tenant_utility
        csrf = _prime_csrf(tenant_client)
        tenant_session_cookie = str(tenant_client.cookies.get("session") or "")
        assert tenant_session_cookie
        forbidden_tenant_host = tenant_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_tenant_host.status_code == 404
        forbidden_password_tenant_host = tenant_client.post(
            f"/platform/tenants/{tenant_a}/admin-password",
            data={
                CSRF_FORM_FIELD: csrf,
                "admin_password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
            follow_redirects=False,
        )
        assert forbidden_password_tenant_host.status_code == 404
        forbidden_create_user_tenant_host = tenant_client.post(
            f"/platform/tenants/{tenant_a}/users",
            data={
                CSRF_FORM_FIELD: csrf,
                "user_email": "new-user@example.com",
                "user_role": ROLE_USER,
                "user_password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
            follow_redirects=False,
        )
        assert forbidden_create_user_tenant_host.status_code == 404

    with _client(app, base_url="https://admin.localhost") as admin_host_client:
        csrf = _prime_csrf(admin_host_client)
        admin_host_client.cookies.set("session", tenant_session_cookie)
        forbidden_non_superadmin = admin_host_client.post(
            f"/platform/tenants/{tenant_a}/disable",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert forbidden_non_superadmin.status_code in {302, 403}
        forbidden_password_non_superadmin = admin_host_client.post(
            f"/platform/tenants/{tenant_a}/admin-password",
            data={
                CSRF_FORM_FIELD: csrf,
                "admin_password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
            follow_redirects=False,
        )
        assert forbidden_password_non_superadmin.status_code in {302, 403}
        forbidden_create_user_non_superadmin = admin_host_client.post(
            f"/platform/tenants/{tenant_a}/users",
            data={
                CSRF_FORM_FIELD: csrf,
                "user_email": "new-user@example.com",
                "user_role": ROLE_USER,
                "user_password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
            follow_redirects=False,
        )
        assert forbidden_create_user_non_superadmin.status_code in {302, 403}

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

    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#773333",
    )
    _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )

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
        assert "default tenant" not in tenants_page.text.lower()
        assert f"/platform/tenants/{demo_tenant}/delete" not in tenants_page.text
        assert f"/platform/tenants/{empty_tenant}/delete" in tenants_page.text
        assert f"/platform/tenants/{busy_tenant}/delete" not in tenants_page.text

        demo_detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert demo_detail.status_code == 200
        assert "Delete is blocked for the demo tenant because it is reserved for internal demo/testing use." in demo_detail.text
        assert "default tenant" not in demo_detail.text.lower()

        busy_detail = admin_client.get(f"/platform/tenants/{busy_tenant}")
        assert busy_detail.status_code == 200
        assert "Delete is blocked because this tenant still has customer records." in busy_detail.text

        csrf = _prime_csrf(admin_client)
        blocked_demo_delete = admin_client.post(
            f"/platform/tenants/{demo_tenant}/delete",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert blocked_demo_delete.status_code in {302, 303}
        assert blocked_demo_delete.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

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
        assert db.get(Tenant, demo_tenant) is not None
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


def test_platform_superadmin_can_update_demo_tenant_admin_email_and_password(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-admin-credentials.db", monkeypatch=monkeypatch
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#6a3d34",
    )
    demo_admin_id = _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

        detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert detail.status_code == 200
        assert "Tenant Admin Email" in detail.text
        assert "Tenant Admin Password" in detail.text
        assert "This also applies to the demo tenant." in detail.text

        csrf = _prime_csrf(admin_client)
        updated_email = admin_client.post(
            f"/platform/tenants/{demo_tenant}/admin-email",
            data={
                CSRF_FORM_FIELD: csrf,
                "admin_email": "demo-updated@example.com",
            },
            follow_redirects=False,
        )
        assert updated_email.status_code in {302, 303}
        assert updated_email.headers.get("location") == f"/platform/tenants/{demo_tenant}?email_saved=1"

        email_page = admin_client.get(updated_email.headers["location"])
        assert email_page.status_code == 200
        assert "Tenant admin email updated." in email_page.text

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="demo-admin@example.com", password="DemoPass123!") == 401
        assert _login(demo_client, email="demo-updated@example.com", password="DemoPass123!") == 303

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        bad_password = admin_client.post(
            f"/platform/tenants/{demo_tenant}/admin-password",
            data={
                CSRF_FORM_FIELD: csrf,
                "admin_password": "NewDemoPass123!",
                "confirm_password": "MismatchPass123!",
            },
            follow_redirects=False,
        )
        assert bad_password.status_code in {302, 303}
        assert bad_password.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        bad_password_page = admin_client.get(bad_password.headers["location"])
        assert bad_password_page.status_code == 200
        assert "Passwords do not match." in bad_password_page.text

        csrf = _prime_csrf(admin_client)
        updated_password = admin_client.post(
            f"/platform/tenants/{demo_tenant}/admin-password",
            data={
                CSRF_FORM_FIELD: csrf,
                "admin_password": "NewDemoPass123!",
                "confirm_password": "NewDemoPass123!",
            },
            follow_redirects=False,
        )
        assert updated_password.status_code in {302, 303}
        assert updated_password.headers.get("location") == f"/platform/tenants/{demo_tenant}?password_saved=1"

        password_page = admin_client.get(updated_password.headers["location"])
        assert password_page.status_code == 200
        assert "Tenant admin password updated." in password_page.text

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="demo-admin@example.com", password="DemoPass123!") == 401
        assert _login(demo_client, email="demo-updated@example.com", password="DemoPass123!") == 401
        assert _login(demo_client, email="demo-updated@example.com", password="NewDemoPass123!") == 303

    with SessionLocal() as db:
        demo_admin = db.get(User, demo_admin_id)
        assert demo_admin is not None
        assert demo_admin.username == "demo-updated@example.com"

        password_user_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "USER_UPDATE",
                AuditEvent.entity_id == str(demo_admin_id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert password_user_event is not None
        assert password_user_event.user_id == superadmin_id
        assert "password" in str(password_user_event.summary or "").lower()

        password_tenant_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "TENANT_UPDATE",
                AuditEvent.entity_id == str(demo_tenant),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert password_tenant_event is not None
        assert password_tenant_event.user_id == superadmin_id
        assert isinstance(password_tenant_event.details_json, dict)
        assert "password" in str(password_tenant_event.summary or "").lower()
        assert "initial_admin_password" in password_tenant_event.details_json.get("changed", {})


def test_platform_superadmin_can_create_demo_user_and_add_more_tenant_users(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-user-create.db", monkeypatch=monkeypatch
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#72443c",
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

        detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert detail.status_code == 200
        assert "No tenant users." in detail.text
        assert f'action="/platform/tenants/{demo_tenant}/users"' in detail.text
        assert "Create the initial demo login after a reset" in detail.text
        assert "Create one above first." in detail.text

        csrf = _prime_csrf(admin_client)
        created_admin = admin_client.post(
            f"/platform/tenants/{demo_tenant}/users",
            data={
                CSRF_FORM_FIELD: csrf,
                "user_email": "demo-admin@example.com",
                "user_role": ROLE_TENANT_ADMIN,
                "user_password": "DemoPass123!",
                "confirm_password": "DemoPass123!",
            },
            follow_redirects=False,
        )
        assert created_admin.status_code in {302, 303}
        assert created_admin.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        created_admin_page = admin_client.get(created_admin.headers["location"])
        assert created_admin_page.status_code == 200
        assert "Tenant user created: demo-admin@example.com." in created_admin_page.text
        assert "demo-admin@example.com" in created_admin_page.text
        assert "Tenant Admin Email" in created_admin_page.text
        assert "Tenant Admin Password" in created_admin_page.text

        csrf = _prime_csrf(admin_client)
        created_operator = admin_client.post(
            f"/platform/tenants/{demo_tenant}/users",
            data={
                CSRF_FORM_FIELD: csrf,
                "user_email": "demo-ops@example.com",
                "user_role": ROLE_USER,
                "user_password": "OperatorPass123!",
                "confirm_password": "OperatorPass123!",
            },
            follow_redirects=False,
        )
        assert created_operator.status_code in {302, 303}
        assert created_operator.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        created_operator_page = admin_client.get(created_operator.headers["location"])
        assert created_operator_page.status_code == 200
        assert "Tenant user created: demo-ops@example.com." in created_operator_page.text
        assert "demo-admin@example.com" in created_operator_page.text
        assert "demo-ops@example.com" in created_operator_page.text

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="demo-admin@example.com", password="DemoPass123!") == 303
        assert _login(demo_client, email="demo-ops@example.com", password="OperatorPass123!") == 303

    with SessionLocal() as db:
        demo_users = list(
            db.execute(
                select(User)
                .where(User.tenant_id == demo_tenant)
                .order_by(User.username.asc())
            ).scalars()
        )
        assert [user.username for user in demo_users] == [
            "demo-admin@example.com",
            "demo-ops@example.com",
        ]
        assert [user.role for user in demo_users] == [
            ROLE_TENANT_ADMIN,
            ROLE_USER,
        ]

        created_events = list(
            db.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.action == "USER_CREATE",
                    AuditEvent.user_id == superadmin_id,
                )
                .order_by(AuditEvent.id.asc())
            ).scalars()
        )
        assert len(created_events) >= 2
        created_emails = {
            str((event.details_json or {}).get("email") or "")
            for event in created_events
        }
        assert "demo-admin@example.com" in created_emails
        assert "demo-ops@example.com" in created_emails


def test_legacy_default_tenant_is_renamed_to_demo_and_hidden_from_platform_ui(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-backfill.db", monkeypatch=monkeypatch
    )
    legacy_tenant = _seed_tenant(
        SessionLocal,
        name="Default Tenant",
        subdomain="default",
        is_active=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=legacy_tenant,
        company_name="Legacy Default Co",
        primary_color="#334455",
    )
    _seed_user(
        SessionLocal,
        email="legacy-default-admin@example.com",
        password="LegacyPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=legacy_tenant,
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

        tenants_page = admin_client.get("/platform/tenants")
        assert tenants_page.status_code == 200
        assert "Default Tenant" not in tenants_page.text
        assert "default tenant" not in tenants_page.text.lower()
        assert "Demo" in tenants_page.text
        assert f"/platform/tenants/{legacy_tenant}/delete" not in tenants_page.text

        detail = admin_client.get(f"/platform/tenants/{legacy_tenant}")
        assert detail.status_code == 200
        assert "<h1>Demo</h1>" in detail.text
        assert "Delete is blocked for the demo tenant because it is reserved for internal demo/testing use." in detail.text
        assert "default tenant" not in detail.text.lower()

    with SessionLocal() as db:
        renamed = db.get(Tenant, legacy_tenant)
        assert renamed is not None
        assert renamed.subdomain == settings.effective_demo_tenant_subdomain
        assert renamed.name == "Demo"
        legacy_lookup = db.execute(
            select(Tenant).where(Tenant.subdomain == "default")
        ).scalars().first()
        assert legacy_lookup is None


def test_demo_tenant_reset_action_is_only_available_for_demo_tenants(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-reset-ui.db", monkeypatch=monkeypatch
    )
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#7a3b2e",
    )
    marked_demo_tenant = _seed_tenant(
        SessionLocal,
        name="Showroom Demo",
        subdomain="showroom",
        is_active=True,
        is_demo=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=marked_demo_tenant,
        company_name="Showroom Demo",
        primary_color="#2d556d",
    )
    non_demo_tenant = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=non_demo_tenant,
        company_name="Tenant A",
        primary_color="#245577",
    )
    _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=non_demo_tenant,
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

        demo_detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert demo_detail.status_code == 200
        assert "Maintenance" in demo_detail.text
        assert "Reset Demo Tenant" in demo_detail.text
        assert "Type DEMO to enable confirmation" in demo_detail.text
        assert f'action="/platform/tenants/{demo_tenant}/reset-demo"' in demo_detail.text

        marked_demo_detail = admin_client.get(f"/platform/tenants/{marked_demo_tenant}")
        assert marked_demo_detail.status_code == 200
        assert f'action="/platform/tenants/{marked_demo_tenant}/reset-demo"' in marked_demo_detail.text
        assert (
            "Delete is blocked for the demo tenant because it is reserved for internal demo/testing use."
            in marked_demo_detail.text
        )

        non_demo_detail = admin_client.get(f"/platform/tenants/{non_demo_tenant}")
        assert non_demo_detail.status_code == 200
        assert f'action="/platform/tenants/{non_demo_tenant}/reset-demo"' not in non_demo_detail.text
        assert "Reset Demo Tenant is only available for workspaces marked as demo." in non_demo_detail.text

        csrf = _prime_csrf(admin_client)
        blocked = admin_client.post(
            f"/platform/tenants/{non_demo_tenant}/reset-demo",
            data={
                CSRF_FORM_FIELD: csrf,
                "confirmation_text": "DEMO",
            },
            follow_redirects=False,
        )
        assert blocked.status_code in {302, 303}
        assert blocked.headers.get("location", "").startswith(f"/platform/tenants/{non_demo_tenant}?")

        blocked_detail = admin_client.get(blocked.headers["location"])
        assert blocked_detail.status_code == 200
        assert "Reset Demo Tenant is only available for workspaces marked as demo." in blocked_detail.text

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as tenant_client:
        assert _login(tenant_client, email="demo-admin@example.com", password="DemoPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        forbidden = tenant_client.post(
            f"/platform/tenants/{demo_tenant}/reset-demo",
            data={
                CSRF_FORM_FIELD: csrf,
                "confirmation_text": "DEMO",
            },
            follow_redirects=False,
        )
        assert forbidden.status_code == 404


def test_platform_superadmin_can_reset_demo_tenant_and_reseed_baseline(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-reset-flow.db", monkeypatch=monkeypatch
    )
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#6b2d2d",
    )
    other_tenant = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=other_tenant,
        company_name="Tenant B",
        primary_color="#2a4f74",
    )

    _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )
    other_admin_id = _seed_user(
        SessionLocal,
        email="tenant-b-admin@example.com",
        password="TenantBPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=other_tenant,
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with SessionLocal() as db:
        db.add(Customer(tenant_id=demo_tenant, account_code="DEMO-001", name="Demo Customer"))
        db.add(Vehicle(tenant_id=demo_tenant, registration="DEMO123"))
        db.add(Customer(tenant_id=other_tenant, account_code="OTHER-001", name="Other Customer"))
        db.add(Vehicle(tenant_id=other_tenant, registration="OTHER123"))
        db.commit()

    demo_logo_dir = uploads_root / "tenants" / str(demo_tenant) / "company"
    demo_logo_dir.mkdir(parents=True, exist_ok=True)
    (demo_logo_dir / "logo.png").write_bytes(b"demo-logo")

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303

        csrf = _prime_csrf(admin_client)
        invalid = admin_client.post(
            f"/platform/tenants/{demo_tenant}/reset-demo",
            data={
                CSRF_FORM_FIELD: csrf,
                "confirmation_text": "WRONG",
            },
            follow_redirects=False,
        )
        assert invalid.status_code in {302, 303}
        assert invalid.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        invalid_page = admin_client.get(invalid.headers["location"])
        assert invalid_page.status_code == 200
        assert "Type DEMO to confirm the reset." in invalid_page.text

    with SessionLocal() as db:
        assert db.execute(select(Customer).where(Customer.tenant_id == demo_tenant)).scalars().first() is not None
        assert db.execute(select(Vehicle).where(Vehicle.tenant_id == demo_tenant)).scalars().first() is not None
        assert db.execute(select(User).where(User.tenant_id == demo_tenant)).scalars().first() is not None

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        reset = admin_client.post(
            f"/platform/tenants/{demo_tenant}/reset-demo",
            data={
                CSRF_FORM_FIELD: csrf,
                "confirmation_text": "DEMO",
            },
            follow_redirects=False,
        )
        assert reset.status_code in {302, 303}
        assert reset.headers.get("location") == f"/platform/tenants/{demo_tenant}?demo_reset=1"

        reset_page = admin_client.get(reset.headers["location"])
        assert reset_page.status_code == 200
        assert "Demo tenant data deleted and recreated." in reset_page.text
        assert "No tenant users." in reset_page.text

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        assert bool(demo.is_active) is True
        assert bool(demo.is_demo) is True

        assert db.execute(select(User).where(User.tenant_id == demo_tenant)).scalars().first() is None
        assert db.execute(select(Customer).where(Customer.tenant_id == demo_tenant)).scalars().first() is None
        assert db.execute(select(Vehicle).where(Vehicle.tenant_id == demo_tenant)).scalars().first() is None

        assert (
            db.execute(select(CompanySetting).where(CompanySetting.tenant_id == demo_tenant))
            .scalars()
            .first()
            is not None
        )
        assert db.execute(select(Yard).where(Yard.tenant_id == demo_tenant)).scalars().first() is not None
        assert db.execute(select(Unit).where(Unit.tenant_id == demo_tenant)).scalars().first() is not None
        assert (
            db.execute(select(PrintTemplate).where(PrintTemplate.tenant_id == demo_tenant))
            .scalars()
            .first()
            is not None
        )
        assert (
            db.execute(select(PrintDestination).where(PrintDestination.tenant_id == demo_tenant))
            .scalars()
            .first()
            is not None
        )
        assert (
            db.execute(
                select(TicketSequence).where(
                    TicketSequence.tenant_id == demo_tenant,
                    TicketSequence.year == int(utcnow().year),
                )
            )
            .scalars()
            .first()
            is not None
        )

        assert db.execute(select(Customer).where(Customer.tenant_id == other_tenant)).scalars().first() is not None
        assert db.execute(select(Vehicle).where(Vehicle.tenant_id == other_tenant)).scalars().first() is not None
        other_admin = db.get(User, other_admin_id)
        assert other_admin is not None
        assert other_admin.tenant_id == other_tenant

        superadmin = db.get(User, superadmin_id)
        assert superadmin is not None
        assert superadmin.tenant_id is None

        reset_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "TENANT_RESET_DEMO",
                AuditEvent.entity_id == str(demo_tenant),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert reset_event is not None
        assert reset_event.user_id == superadmin_id
        assert reset_event.tenant_id is None

    assert not (uploads_root / "tenants" / str(demo_tenant)).exists()


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
        platform_nav = _extract_nav_markup(tenants_page.text)
        platform_utility = _extract_utility_bar_markup(tenants_page.text)
        assert ">Tenant Management<" in tenants_page.text
        assert ">System Status<" in tenants_page.text
        assert ">Tickets<" not in tenants_page.text
        assert ">Customers<" not in tenants_page.text
        assert ">Vehicles<" not in tenants_page.text
        assert ">Products<" not in tenants_page.text
        assert ">Invoices<" not in tenants_page.text
        assert ">Lookups<" not in tenants_page.text
        assert ">Reports<" not in tenants_page.text
        assert "Platform Admin" not in platform_nav
        assert "Signed in as superadmin@example.com" not in platform_nav
        assert "Logout" not in platform_nav
        assert "LOCALHOST" in tenants_page.text
        assert "Platform Admin" in platform_utility
        assert "Signed in as superadmin@example.com" in platform_utility
        assert "Logout" in platform_utility

        blocked_tickets = admin_client.get("/tickets")
        assert blocked_tickets.status_code == 404
        assert "Unknown tenant" in blocked_tickets.text


def test_tenant_settings_hides_platform_tools_and_keeps_platform_routes_separate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "uploads_dir", str((tmp_path / "uploads").resolve()))
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-settings-cleanup.db", monkeypatch=monkeypatch
    )
    _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=1,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TenantPass123!") == 303
        settings_page = tenant_client.get("/admin")
        assert settings_page.status_code == 200
        tenant_nav = _extract_nav_markup(settings_page.text)
        assert '>Home<' in tenant_nav
        assert tenant_nav.index('>Home<') < tenant_nav.index('>Tickets<')
        assert ">Settings<" in tenant_nav
        assert ">Admin<" not in tenant_nav
        assert "Setup & Configuration" in settings_page.text
        assert "Operations" in settings_page.text
        assert "Support" in settings_page.text
        assert "Company" in settings_page.text
        assert "EWC Codes" in settings_page.text
        assert "Printing" in settings_page.text
        assert "Audit Log" in settings_page.text
        assert "Help" in settings_page.text
        assert "Manage Company" in settings_page.text
        assert "Manage EWC Codes" in settings_page.text
        assert "View Audit Log" in settings_page.text
        assert "Open Help" in settings_page.text
        assert "Open Company" not in settings_page.text
        assert "Open EWC Codes" not in settings_page.text
        assert "Open Audit Log" not in settings_page.text
        assert "System Status" not in settings_page.text
        assert "DEV mode" not in settings_page.text
        assert "Turn DEV Mode" not in settings_page.text
        assert "Tenant Management" not in settings_page.text
        assert tenant_client.get("/admin/system-status").status_code == 404
        tenant_csrf = str(tenant_client.cookies.get(CSRF_COOKIE_NAME) or "")
        assert tenant_csrf
        forbidden_toggle = tenant_client.post(
            "/admin/dev-mode",
            data={"enabled": "1", CSRF_FORM_FIELD: tenant_csrf},
            follow_redirects=False,
        )
        assert forbidden_toggle.status_code == 404

    original_dev_mode = bool(templates.env.globals.get("DEV_MODE", False))
    try:
        with _client(app, base_url="https://admin.localhost") as admin_client:
            assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
            system_status = admin_client.get("/admin/system-status")
            assert system_status.status_code == 200
            admin_csrf = _prime_csrf(admin_client)
            toggle = admin_client.post(
                "/admin/dev-mode",
                data={
                    "enabled": "0" if original_dev_mode else "1",
                    CSRF_FORM_FIELD: admin_csrf,
                },
                follow_redirects=False,
            )
            assert toggle.status_code == 303
    finally:
        templates.env.globals["DEV_MODE"] = original_dev_mode


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

        marketing_reserved = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "Reserved Software",
                "subdomain": "software",
                "admin_email": "reserved-software@example.com",
                "admin_password": "Reserved123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert marketing_reserved.status_code == 400
        assert "Subdomain is reserved." in marketing_reserved.text

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
    demo_tenant = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=demo_tenant,
        company_name="Demo Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
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

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.example.test") as demo_client:
        assert (
            _login(
                demo_client,
                email="demo-admin@example.com",
                password="TenantPass123!",
            )
            == 303
        )
        assert demo_client.get("/tickets").status_code == 200


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
    assert "--dashboard-chart-bar-end: #111111;" in branding_a
    assert "--dashboard-chart-bar-end: #0055CC;" in branding_b
    assert "--dashboard-throughput-bar-start: #111111;" in branding_a
    assert "--dashboard-throughput-bar-start: #0055CC;" in branding_b

    with SessionLocal() as db:
        tenant_b_obj = db.get(Tenant, tenant_b)
        assert tenant_b_obj is not None
        tenant_b_obj.is_active = False
        db.commit()

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert tenant_b_client.get("/health").status_code == 403


def test_logged_in_tenant_home_shows_dashboard_empty_state(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-empty.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(SessionLocal, name="Dashboard Co", subdomain="dash")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Dashboard Co",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="dash-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with _client(app, base_url="https://dash.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="dash-admin@example.com",
                password="TestPass123!",
                next_path="/",
            )
            == 303
        )
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert "Operations Dashboard" in response.text
    assert "Setup complete. System initialization checks are green." not in response.text
    assert response.text.count('data-dashboard-metric="') == 4
    assert 'data-dashboard-panel="todays-traffic"' in response.text
    assert 'data-dashboard-panel="weight-throughput"' in response.text
    assert 'data-dashboard-period="today"' in response.text
    assert 'data-dashboard-period="7d"' in response.text
    assert 'data-dashboard-period="30d"' in response.text
    assert 'data-dashboard-period-active="1"' in response.text
    assert "Activity Overview" in response.text
    assert "Updated just now" in response.text
    assert "Overview Activity" not in response.text
    assert 'data-dashboard-empty-state="1"' in response.text
    assert "No operational activity yet" in response.text
    assert "Awaiting completion" in response.text
    assert "Currently awaiting completion" not in response.text
    assert "No completed tickets yet today." in response.text
    assert "No completed ticket weight recorded for this period." in response.text
    assert "Open tickets currently awaiting completion." not in response.text
    assert 'data-dashboard-panel="invoice-activity"' in response.text
    assert "No invoice activity recorded for last 7 days." in response.text
    assert "Create Ticket" in response.text
    assert 'href="/tickets/new"' in response.text
    assert 'href="/customers/new"' in response.text
    assert 'href="/vehicles/new"' in response.text
    assert _dashboard_metric_value(response.text, "open_tickets") == "0"
    assert _dashboard_metric_value(response.text, "completed_today") == "0"
    assert _dashboard_metric_value(response.text, "invoices_pending") == "0"


def test_logged_in_tenant_home_dashboard_is_tenant_scoped_and_populated(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-data.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#225577",
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

    with SessionLocal() as db:
        today = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        five_days_ago = today - timedelta(days=5)
        twenty_days_ago = today - timedelta(days=20)

        customer_a = Customer(tenant_id=tenant_a, account_code="CUST-A", name="Tenant A Customer")
        customer_b = Customer(tenant_id=tenant_b, account_code="CUST-B", name="Tenant B Customer")
        vehicle_a = Vehicle(tenant_id=tenant_a, registration="A123 DASH")
        vehicle_b = Vehicle(tenant_id=tenant_b, registration="B123 OTHER")
        product_a = Product(tenant_id=tenant_a, code="PROD-A", description="Tenant A Product", unit_price=Decimal("12.50"))
        product_b = Product(tenant_id=tenant_b, code="PROD-B", description="Tenant B Product", unit_price=Decimal("10.00"))
        db.add_all([customer_a, customer_b, vehicle_a, vehicle_b, product_a, product_b])
        db.flush()

        db.add_all(
            [
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-OPEN-1",
                    datetime=today,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    product_id=product_a.id,
                    net_kg=1250,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-COMP-1",
                    datetime=today - timedelta(hours=1),
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    product_id=product_a.id,
                    net_kg=1500,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-COMP-OLD",
                    datetime=five_days_ago,
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.OUTWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEOUT.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    product_id=product_a.id,
                    net_kg=2200,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-YDAY-1",
                    datetime=yesterday,
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    product_id=product_a.id,
                    net_kg=1750,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-20D-1",
                    datetime=twenty_days_ago,
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    product_id=product_a.id,
                    net_kg=3100,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_b,
                    ticket_no="B-ONLY-1",
                    datetime=yesterday,
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_b.id,
                    vehicle_id=vehicle_b.id,
                    product_id=product_b.id,
                    net_kg=9999,
                    dont_invoice=False,
                    paid=False,
                ),
                Invoice(
                    tenant_id=tenant_a,
                    invoice_no="INV-A-1",
                    customer_id=customer_a.id,
                    invoice_date=today.date(),
                    status="OPEN",
                    net_total=Decimal("100.00"),
                    vat_total=Decimal("20.00"),
                    gross_total=Decimal("120.00"),
                ),
                Invoice(
                    tenant_id=tenant_a,
                    invoice_no="INV-A-OLD",
                    customer_id=customer_a.id,
                    invoice_date=twenty_days_ago.date(),
                    status="SENT",
                    net_total=Decimal("80.00"),
                    vat_total=Decimal("16.00"),
                    gross_total=Decimal("96.00"),
                ),
                Invoice(
                    tenant_id=tenant_b,
                    invoice_no="INV-B-1",
                    customer_id=customer_b.id,
                    invoice_date=today.date(),
                    status="OPEN",
                    net_total=Decimal("90.00"),
                    vat_total=Decimal("18.00"),
                    gross_total=Decimal("108.00"),
                ),
            ]
        )
        db.commit()

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="a-admin@example.com",
                password="TestPass123!",
                next_path="/",
            )
            == 303
        )
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert "Operations Dashboard" in response.text
    assert "Setup complete. System initialization checks are green." not in response.text
    assert response.text.count('data-dashboard-metric="') == 4
    assert 'data-dashboard-empty-state="1"' not in response.text
    assert 'data-dashboard-panel="weight-throughput"' in response.text
    assert 'data-dashboard-panel="invoice-activity"' in response.text
    assert _dashboard_metric_value(response.text, "open_tickets") == "1"
    assert _dashboard_metric_value(response.text, "completed_today") == "1"
    assert _dashboard_metric_value(response.text, "total_weight_today") == "1,500 kg"
    assert _dashboard_metric_value(response.text, "invoices_pending") == "2"
    assert "Total processed this period: 5.5 tonnes" in response.text
    assert 'data-dashboard-ticket="A-COMP-1"' in response.text
    assert 'data-dashboard-ticket="A-OPEN-1"' in response.text
    assert 'data-dashboard-open-ticket="A-OPEN-1"' in response.text
    assert 'data-dashboard-traffic-ticket="A-COMP-1"' in response.text
    assert 'data-dashboard-throughput-kg="1500"' in response.text
    assert 'data-dashboard-throughput-kg="1750"' in response.text
    assert 'data-dashboard-throughput-kg="2200"' in response.text
    assert "09:00" in response.text
    assert 'data-dashboard-invoice="INV-A-1"' in response.text
    assert 'data-dashboard-invoice-ready-ticket="A-COMP-1"' in response.text
    assert "Tenant A Customer" in response.text
    assert "Tenant B Customer" not in response.text
    assert 'data-dashboard-traffic-ticket="A-YDAY-1"' not in response.text
    assert 'data-dashboard-traffic-ticket="A-OPEN-1"' not in response.text
    assert 'data-dashboard-ticket="A-20D-1"' not in response.text
    assert 'data-dashboard-throughput-kg="3100"' not in response.text
    assert 'data-dashboard-invoice="INV-A-OLD"' not in response.text
    assert 'data-dashboard-ticket="B-ONLY-1"' not in response.text
    assert 'data-dashboard-open-ticket="B-ONLY-1"' not in response.text
    assert 'data-dashboard-traffic-ticket="B-ONLY-1"' not in response.text
    assert 'data-dashboard-throughput-kg="9999"' not in response.text
    assert 'data-dashboard-invoice="INV-B-1"' not in response.text
    assert 'data-dashboard-invoice-ready-ticket="B-ONLY-1"' not in response.text
    assert "INV-B-1" not in response.text

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="a-admin@example.com",
                password="TestPass123!",
                next_path="/?period=today",
            )
            == 303
        )
        response_today = tenant_client.get("/?period=today")

    assert response_today.status_code == 200
    assert 'data-dashboard-period="today"' in response_today.text
    assert 'data-dashboard-period-active="1"' in response_today.text
    assert "Total processed this period: 1.5 tonnes" in response_today.text
    assert 'data-dashboard-throughput-kg="1500"' in response_today.text
    assert 'data-dashboard-throughput-kg="1750"' not in response_today.text
    assert 'data-dashboard-throughput-kg="2200"' not in response_today.text
    assert 'data-dashboard-throughput-kg="3100"' not in response_today.text

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="a-admin@example.com",
                password="TestPass123!",
                next_path="/?period=30d",
            )
            == 303
        )
        response_30d = tenant_client.get("/?period=30d")

    assert response_30d.status_code == 200
    assert 'data-dashboard-period="30d"' in response_30d.text
    assert 'data-dashboard-period-active="1"' in response_30d.text
    assert "Total processed this period: 8.6 tonnes" in response_30d.text
    assert 'data-dashboard-throughput-kg="3100"' in response_30d.text
    assert 'data-dashboard-ticket="A-20D-1"' in response_30d.text
    assert 'data-dashboard-invoice="INV-A-OLD"' in response_30d.text


def test_all_tenant_subdomains_use_dashboard_on_root_and_non_tenant_hosts_do_not(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_domain", "example.test")
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-hosts.db", monkeypatch=monkeypatch
    )
    mjteale_id = _seed_tenant(SessionLocal, name="MJ Teale Ltd.", subdomain="mjteale")
    lotus_id = _seed_tenant(SessionLocal, name="Lotus Cars Ltd.", subdomain="lotus")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=mjteale_id,
        company_name="MJ Teale Ltd.",
        primary_color="#0f4c81",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=lotus_id,
        company_name="Lotus Cars Ltd.",
        primary_color="#235d3a",
    )
    _seed_user(
        SessionLocal,
        email="mjteale-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=mjteale_id,
    )
    _seed_user(
        SessionLocal,
        email="lotus-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=lotus_id,
    )

    with SessionLocal() as db:
        customer = Customer(
            tenant_id=mjteale_id,
            account_code="MJ-CUST-1",
            name="MJ Teale Customer",
        )
        vehicle = Vehicle(tenant_id=mjteale_id, registration="MJ24 TST")
        product = Product(
            tenant_id=mjteale_id,
            code="MJ-PROD-1",
            description="Screened Aggregate",
            unit_price=Decimal("15.00"),
        )
        db.add_all([customer, vehicle, product])
        db.flush()
        db.add(
            Ticket(
                tenant_id=mjteale_id,
                ticket_no="MJ-DASH-1",
                datetime=utcnow().replace(hour=9, minute=15, second=0, microsecond=0),
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                product_id=product.id,
                net_kg=1800,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

    with _client(app, base_url="https://lotus.example.test") as lotus_client:
        lotus_root = lotus_client.get("/", follow_redirects=False)
        assert lotus_root.status_code in {302, 303}
        assert lotus_root.headers.get("location", "").startswith("/login")
        assert (
            _login(
                lotus_client,
                email="lotus-admin@example.com",
                password="TestPass123!",
                next_path="/",
            )
            == 303
        )
        lotus_dashboard = lotus_client.get("/")
        assert lotus_dashboard.status_code == 200
        assert "Operations Dashboard" in lotus_dashboard.text
        assert 'data-dashboard-empty-state="1"' in lotus_dashboard.text
        assert "No operational activity yet" in lotus_dashboard.text

    with _client(app, base_url="https://mjteale.example.test") as mjteale_client:
        mjteale_root = mjteale_client.get("/", follow_redirects=False)
        assert mjteale_root.status_code in {302, 303}
        assert mjteale_root.headers.get("location", "").startswith("/login")
        assert (
            _login(
                mjteale_client,
                email="mjteale-admin@example.com",
                password="TestPass123!",
                next_path="/",
            )
            == 303
        )
        mjteale_dashboard = mjteale_client.get("/")
        assert mjteale_dashboard.status_code == 200
        assert "Operations Dashboard" in mjteale_dashboard.text
        assert 'data-dashboard-empty-state="1"' not in mjteale_dashboard.text
        assert 'data-dashboard-ticket="MJ-DASH-1"' in mjteale_dashboard.text
        assert _dashboard_metric_value(mjteale_dashboard.text, "open_tickets") == "1"
        assert "Lotus Cars Ltd." not in mjteale_dashboard.text

    with _client(app, base_url="https://software.example.test") as marketing_client:
        marketing = marketing_client.get("/")
        assert marketing.status_code == 200
        assert "Cloud software for weighbridge operations." in marketing.text
        assert "Operations Dashboard" not in marketing.text

    with _client(app, base_url="https://admin.example.test") as admin_client:
        admin_root = admin_client.get("/", follow_redirects=False)
        assert admin_root.status_code in {302, 303}
        assert admin_root.headers.get("location") == "/platform/tenants"


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


def test_print_logo_helpers_stay_tenant_scoped_when_filenames_collide(
    tmp_path,
    monkeypatch,
):
    _app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-print-logo-collision.db",
        monkeypatch=monkeypatch,
    )
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#225577",
    )

    collision_name = "shared-print-logo.png"
    collision_url = f"/static/uploads/company/{collision_name}"
    logo_a = uploads_root / "tenants" / str(tenant_a) / "company" / collision_name
    logo_b = uploads_root / "tenants" / str(tenant_b) / "company" / collision_name
    logo_a.parent.mkdir(parents=True, exist_ok=True)
    logo_b.parent.mkdir(parents=True, exist_ok=True)
    logo_a.write_bytes(b"\x89PNG\r\n\x1a\ntenant-a-print-logo")
    logo_b.write_bytes(b"\x89PNG\r\n\x1a\ntenant-b-print-logo")

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

    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_a
        db.info["platform_mode"] = False
        context_a = build_print_base_context(db)
        payload_logo_a = _company_logo_src(db)

    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_b
        db.info["platform_mode"] = False
        context_b = build_print_base_context(db)
        payload_logo_b = _company_logo_src(db)

    context_logo_a = str(context_a.get("company_logo_url") or "")
    context_logo_b = str(context_b.get("company_logo_url") or "")
    assert context_logo_a.startswith("data:image/png;base64,")
    assert context_logo_b.startswith("data:image/png;base64,")
    assert context_logo_a != context_logo_b
    assert payload_logo_a.startswith("data:image/png;base64,")
    assert payload_logo_b.startswith("data:image/png;base64,")
    assert payload_logo_a != payload_logo_b


def test_tenant_hosts_use_uploaded_logo_for_html_favicon_and_favicon_route(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-favicon.db", monkeypatch=monkeypatch
    )
    uploads_root = (tmp_path / "uploads").resolve()
    monkeypatch.setattr(settings, "uploads_dir", str(uploads_root))

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a")
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_b,
        company_name="Tenant B Co",
        primary_color="#225577",
    )

    logo_a_name = "tenant-a-favicon.png"
    logo_b_name = "tenant-b-favicon.png"
    logo_a_bytes = b"\x89PNG\r\n\x1a\ntenant-a-favicon"
    logo_b_bytes = b"\x89PNG\r\n\x1a\ntenant-b-favicon"
    logo_a_file = uploads_root / "tenants" / str(tenant_a) / "company" / logo_a_name
    logo_b_file = uploads_root / "tenants" / str(tenant_b) / "company" / logo_b_name
    logo_a_file.parent.mkdir(parents=True, exist_ok=True)
    logo_b_file.parent.mkdir(parents=True, exist_ok=True)
    logo_a_file.write_bytes(logo_a_bytes)
    logo_b_file.write_bytes(logo_b_bytes)

    with SessionLocal() as db:
        company_a = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_a).limit(1)
        ).scalars().first()
        company_b = db.execute(
            select(CompanySetting).where(CompanySetting.tenant_id == tenant_b).limit(1)
        ).scalars().first()
        assert company_a is not None and company_b is not None
        company_a.company_logo_path = f"/static/uploads/company/{logo_a_name}"
        company_b.company_logo_path = f"/static/uploads/company/{logo_b_name}"
        company_a.company_logo_updated_at = datetime(2026, 3, 8, 12, 0, 0)
        company_b.company_logo_updated_at = datetime(2026, 3, 8, 12, 5, 0)
        db.commit()

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        login_page = tenant_a_client.get("/login")
        assert login_page.status_code == 200
        assert 'rel="icon"' in login_page.text
        assert 'type="image/png"' in login_page.text
        assert 'rel="shortcut icon"' in login_page.text
        assert f"/static/uploads/company/{logo_a_name}?v=" in login_page.text
        assert f"/static/uploads/company/{logo_b_name}?v=" not in login_page.text

        favicon_redirect = tenant_a_client.get("/favicon.ico", follow_redirects=False)
        assert favicon_redirect.status_code == 307
        assert f"/static/uploads/company/{logo_a_name}?v=" in favicon_redirect.headers.get(
            "location", ""
        )

        favicon_response = tenant_a_client.get("/favicon.ico")
        assert favicon_response.status_code == 200
        assert favicon_response.content == logo_a_bytes

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        login_page = tenant_b_client.get("/login")
        assert login_page.status_code == 200
        assert 'rel="icon"' in login_page.text
        assert 'type="image/png"' in login_page.text
        assert f"/static/uploads/company/{logo_b_name}?v=" in login_page.text
        assert f"/static/uploads/company/{logo_a_name}?v=" not in login_page.text

        favicon_redirect = tenant_b_client.get("/favicon.ico", follow_redirects=False)
        assert favicon_redirect.status_code == 307
        assert f"/static/uploads/company/{logo_b_name}?v=" in favicon_redirect.headers.get(
            "location", ""
        )

        favicon_response = tenant_b_client.get("/favicon.ico")
        assert favicon_response.status_code == 200
        assert favicon_response.content == logo_b_bytes


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
        assert tenant_client.get("/admin/ewc-codes").status_code == 200

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        assert admin_client.get("/admin/ewc-codes").status_code == 200
