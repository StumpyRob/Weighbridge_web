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
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.main as main_module
import app.services.ai_assistant as ai_assistant_module
import app.services.ai_assistant_data as ai_assistant_data_module
import app.services.ai_usage as ai_usage_module
import app.services.email_service as email_service_module
import app.services.platform_ai_settings as platform_ai_settings_module
from app.auth import ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_USER, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import (
    AIUsageLog,
    AuditEvent,
    Area,
    Base,
    CompanySetting,
    Container,
    Customer,
    CustomerProductPrice,
    Destination,
    DirectionEnum,
    Driver,
    EwcCode,
    Haulier,
    Invoice,
    InvoiceSequence,
    InvoiceLine,
    PrintAgent,
    PrintAgentPairing,
    PrintDestination,
    PrintJob,
    PrintTemplate,
    Product,
    PlatformSetting,
    TaxRate,
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
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.services.credit import customer_outstanding_total
from app.services.demo_dataset import DEMO_SIGNATURE_DATA_URI
from app.services.demo_tenant_reset import format_demo_reset_datetime, next_demo_reset_at
from app.services.print_context import build_print_base_context
from app.services.print_payload import _company_logo_src
from app.services.signatures import normalize_png_data_url, png_has_visible_ink
from app.services.system_setup import (
    DEFAULT_YARD_NAME,
    ensure_company_settings_row_exists,
    seed_required_reference_data,
    upsert_default_yard,
)
from app.templating import templates
from app.tenancy import current_platform_mode, current_tenant_id
from app.user_roles import ROLE_ACCOUNTS, ROLE_OPERATOR


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


def _login_platform_superadmin(client: TestClient, *, email: str = "superadmin@example.com") -> None:
    assert _login(
        client,
        email=email,
        password="TestPass123!",
        next_path="/platform/tenants",
    ) == 303


def _post_with_csrf(client: TestClient, path: str, *, data: dict[str, object] | None = None):
    payload = {CSRF_FORM_FIELD: _prime_csrf(client)}
    if data:
        payload.update(data)
    return client.post(path, data=payload, follow_redirects=False)


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: int | None,
    first_name: str = "",
    last_name: str = "",
    is_active: bool = True,
) -> int:
    with SessionLocal() as db:
        user = User(
                **user_identity_kwargs(email=email, role=role),
                first_name=first_name or None,
                last_name=last_name or None,
                password_hash=hash_password(password),
                is_active=is_active,
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


def _tenant_row_count(db, model, tenant_id: int) -> int:
    return int(
        db.execute(
            select(func.count(model.id)).where(model.tenant_id == int(tenant_id))
        ).scalar_one()
        or 0
    )


def _seed_tenant(
    SessionLocal: sessionmaker,
    *,
    name: str,
    subdomain: str,
    is_active: bool = True,
    is_demo: bool = False,
    ai_enabled: bool = False,
    ai_model: str | None = None,
    ai_dashboard_insights_override: bool | None = None,
    demo_reset_interval_days: int | None = None,
    demo_reset_time_minutes: int | None = None,
    demo_last_reset_at: datetime | None = None,
) -> int:
    with SessionLocal() as db:
        tenant = Tenant(
            name=name,
            subdomain=subdomain,
            is_active=is_active,
            is_demo=is_demo,
            ai_enabled=ai_enabled,
            ai_model=ai_model,
            ai_dashboard_insights_override=ai_dashboard_insights_override,
            demo_reset_interval_days=demo_reset_interval_days,
            demo_reset_time_minutes=demo_reset_time_minutes,
            demo_last_reset_at=demo_last_reset_at,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _save_platform_ai_settings(SessionLocal: sessionmaker, **overrides) -> None:
    defaults = platform_ai_settings_module.platform_ai_settings_defaults()
    values = {
        "default_ai_model": overrides.get("default_ai_model", defaults.default_ai_model),
        "ai_temperature": overrides.get("ai_temperature", defaults.ai_temperature),
        "ai_max_output_tokens": overrides.get(
            "ai_max_output_tokens",
            defaults.ai_max_output_tokens,
        ),
        "ai_dashboard_insights_enabled": overrides.get(
            "ai_dashboard_insights_enabled",
            defaults.ai_dashboard_insights_enabled,
        ),
        "ai_dashboard_cache_ttl_seconds": overrides.get(
            "ai_dashboard_cache_ttl_seconds",
            defaults.ai_dashboard_cache_ttl_seconds,
        ),
        "assistant_requests_per_user_per_hour": overrides.get(
            "assistant_requests_per_user_per_hour",
            defaults.assistant_requests_per_user_per_hour,
        ),
        "assistant_requests_per_tenant_per_hour": overrides.get(
            "assistant_requests_per_tenant_per_hour",
            defaults.assistant_requests_per_tenant_per_hour,
        ),
        "dashboard_insights_min_refresh_seconds": overrides.get(
            "dashboard_insights_min_refresh_seconds",
            defaults.dashboard_insights_min_refresh_seconds,
        ),
        "dashboard_insights_max_per_tenant_per_hour": overrides.get(
            "dashboard_insights_max_per_tenant_per_hour",
            defaults.dashboard_insights_max_per_tenant_per_hour,
        ),
        "ai_default_response_style": overrides.get(
            "ai_default_response_style",
            defaults.ai_default_response_style,
        ),
        "ai_default_focus": overrides.get(
            "ai_default_focus",
            defaults.ai_default_focus,
        ),
        "ai_extra_global_instructions": overrides.get(
            "ai_extra_global_instructions",
            defaults.ai_extra_global_instructions,
        ),
    }
    with SessionLocal() as db:
        state = platform_ai_settings_module.validate_platform_ai_settings(**values)
        platform_ai_settings_module.save_platform_ai_settings(db, state)
        db.commit()


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
    demo_tenant_id = _seed_tenant(
        SessionLocal,
        name="Demo",
        subdomain=settings.effective_demo_tenant_subdomain,
        is_active=True,
        is_demo=True,
    )
    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant_id)
        assert demo is not None
        demo.demo_reset_interval_days = 3
        demo.demo_reset_time_minutes = (5 * 60) + 30
        demo.demo_last_reset_at = utcnow().replace(second=0, microsecond=0)
        expected_next_reset = format_demo_reset_datetime(next_demo_reset_at(demo))
        db.commit()

    with _client(app, base_url="https://example.test") as apex_client:
        landing_redirect = apex_client.get("/", follow_redirects=False)
        assert landing_redirect.status_code == 307
        assert landing_redirect.headers.get("location") == "https://software.example.test/"

    with _client(app, base_url="https://software.example.test") as marketing_client:
        landing = marketing_client.get("/")
        assert landing.status_code == 200
        assert "Weighbridge Web" in landing.text
        assert "Cloud software for weighbridge operations." in landing.text
        assert "Try the demo" in landing.text
        assert "Cloud software for ticketing, compliance, and invoicing." not in landing.text
        assert 'name="description"' in landing.text
        assert 'property="og:title"' in landing.text
        assert 'property="og:description"' in landing.text
        assert "/static/css/marketing.css" in landing.text
        assert "https://software.example.test/" in landing.text
        assert 'href="https://demo.example.test/"' in landing.text
        assert expected_next_reset in landing.text
        assert "Shared demo data can be changed by other visitors" in landing.text

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


def test_tenant_audit_page_only_shows_current_workspace_events(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-audit-scope.db", monkeypatch=monkeypatch
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

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_a_client)
        response = tenant_a_client.post(
            "/admin/company",
            data={
                "name": "Tenant A Co Updated",
                "login_email": "a-admin@example.com",
                "navbar_color_hex": "#113355",
                "primary_color_hex": "#336699",
                "nav_logo_height_px": "34",
                "show_nav_logo": "1",
                "show_nav_title": "1",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="b-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_b_client)
        response = tenant_b_client.post(
            "/admin/company",
            data={
                "name": "Tenant B Co Updated",
                "login_email": "b-admin@example.com",
                "navbar_color_hex": "#225577",
                "primary_color_hex": "#557799",
                "nav_logo_height_px": "34",
                "show_nav_logo": "1",
                "show_nav_title": "1",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

    with SessionLocal() as db:
        settings_events = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "UPDATE",
                AuditEvent.entity_type == "company_setting",
            )
            .order_by(AuditEvent.id.asc())
        ).scalars().all()
        assert any(str(event.tenant_id or "") == str(tenant_a) for event in settings_events)
        assert any(str(event.tenant_id or "") == str(tenant_b) for event in settings_events)

    with _client(app, base_url="https://a.localhost") as tenant_a_client:
        assert _login(tenant_a_client, email="a-admin@example.com", password="TestPass123!") == 303
        audit_page = tenant_a_client.get("/admin/audit?entity_type=company_setting&range=all")
        assert audit_page.status_code == 200
        assert "a-admin@example.com" in audit_page.text
        assert "b-admin@example.com" not in audit_page.text
        assert "company_setting #" in audit_page.text

    with _client(app, base_url="https://b.localhost") as tenant_b_client:
        assert _login(tenant_b_client, email="b-admin@example.com", password="TestPass123!") == 303
        audit_page = tenant_b_client.get("/admin/audit?entity_type=company_setting&range=all")
        assert audit_page.status_code == 200
        assert "b-admin@example.com" in audit_page.text
        assert "a-admin@example.com" not in audit_page.text
        assert "company_setting #" in audit_page.text


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
        assert "Disabled" in tenants_page.text
        assert "/t/a/login" in tenants_page.text
        assert 'name="csrf_token"' in tenants_page.text

        tenant_detail = admin_client.get(f"/platform/tenants/{tenant_a}")
        assert tenant_detail.status_code == 200
        assert "Tenant A" in tenant_detail.text
        assert "Tenant access, primary admin, and tenant-scoped users." in tenant_detail.text
        section_positions = [
            tenant_detail.text.index("<h2>Tenant Summary"),
            tenant_detail.text.index("<h2>Primary Tenant Admin"),
            tenant_detail.text.index("<h2>Users"),
            tenant_detail.text.index("<h2>Add User"),
            tenant_detail.text.index("<h2>Tenant AI Overrides"),
            tenant_detail.text.index("<h2>Maintenance"),
        ]
        assert section_positions == sorted(section_positions)
        assert 'id="platform-primary-tenant-admin-help"' in tenant_detail.text
        assert 'id="platform-tenant-users-help"' in tenant_detail.text
        assert 'id="platform-add-tenant-user-help"' in tenant_detail.text
        assert 'id="platform-add-user-role-help"' in tenant_detail.text
        assert "tenant-admin@example.com" in tenant_detail.text
        assert "Tenant Admin" in tenant_detail.text
        assert "Operator" in tenant_detail.text
        assert "Accounts" in tenant_detail.text
        assert "Read Only" in tenant_detail.text
        assert 'href="/t/a/login"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/users"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/admin-email"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/admin-password"' in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/delete"' in tenant_detail.text
        assert 'name="first_name"' in tenant_detail.text
        assert 'name="last_name"' in tenant_detail.text
        assert 'name="email"' in tenant_detail.text
        assert 'name="role"' in tenant_detail.text

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
        assert 'id="site-sidebar"' in tenant_admin_page.text
        assert 'data-shell-toggle' in tenant_admin_page.text
        assert 'data-shell-backdrop' in tenant_admin_page.text
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


def test_platform_superadmin_can_update_tenant_ai_settings(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-settings-admin.db", monkeypatch=monkeypatch
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(
            admin_client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/tenants",
        ) == 303
        csrf = _prime_csrf(admin_client)

        tenant_detail = admin_client.get(f"/platform/tenants/{tenant_a}")
        assert tenant_detail.status_code == 200
        assert "Tenant AI Overrides" in tenant_detail.text
        assert f'action="/platform/tenants/{tenant_a}/ai-settings"' in tenant_detail.text
        assert "AI assistant enabled" in tenant_detail.text
        assert 'id="tenant-ai-overrides-help"' in tenant_detail.text
        assert "AI model override" in tenant_detail.text
        assert 'id="tenant-ai-model-override-help"' in tenant_detail.text
        assert "Dashboard insights override" in tenant_detail.text
        assert 'id="tenant-ai-dashboard-override-help"' in tenant_detail.text
        assert "Use platform default (gpt-5-mini)" in tenant_detail.text
        assert "Use platform default (Enabled)" in tenant_detail.text
        assert 'option value="gpt-5"' in tenant_detail.text

        update_ai = admin_client.post(
            f"/platform/tenants/{tenant_a}/ai-settings",
            data={
                CSRF_FORM_FIELD: csrf,
                "ai_enabled": "1",
                "ai_model_override": "gpt-5",
                "ai_dashboard_insights_override": "disabled",
            },
            follow_redirects=False,
        )
        assert update_ai.status_code in {302, 303}
        assert update_ai.headers.get("location") == f"/platform/tenants/{tenant_a}?ai_saved=1"

        updated_detail = admin_client.get(update_ai.headers["location"])
        assert updated_detail.status_code == 200
        assert "AI settings updated." in updated_detail.text
        assert 'value="gpt-5" selected' in updated_detail.text
        assert 'value="disabled"' in updated_detail.text
        assert 'id="tenant-ai-model-override-help"' in updated_detail.text
        assert 'id="tenant-ai-dashboard-override-help"' in updated_detail.text

    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_a)
        assert tenant is not None
        assert bool(tenant.ai_enabled) is True
        assert tenant.ai_model == "gpt-5"
        assert tenant.ai_dashboard_insights_override is False
        audit_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "TENANT_UPDATE",
                AuditEvent.entity_id == str(tenant_a),
                AuditEvent.summary == "Updated AI settings for tenant Tenant A",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert audit_event is not None
        assert audit_event.user_id == superadmin_id
        assert isinstance(audit_event.details_json, dict)
        assert audit_event.details_json.get("changed", {}).get("ai_enabled", {}).get("to") is True
        assert (
            audit_event.details_json.get("changed", {}).get("ai_model_override", {}).get("to")
            == "gpt-5"
        )
        assert (
            audit_event.details_json.get("changed", {})
            .get("ai_dashboard_insights_override", {})
            .get("to")
            is False
        )


def test_platform_superadmin_can_update_platform_ai_settings(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="platform-ai-settings-update.db", monkeypatch=monkeypatch
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(
            admin_client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/tenants",
        ) == 303

        page = admin_client.get("/platform/ai-settings")
        assert page.status_code == 200
        assert ">Platform AI Defaults<" in page.text
        assert "Platform-level AI defaults for tenant workspaces. Tenant Management can override these per tenant." in page.text
        assert 'action="/platform/ai-settings"' in page.text
        assert 'action="/platform/ai-settings/reset"' in page.text
        assert 'id="platform-ai-default-settings-help"' in page.text
        assert "Default AI model" in page.text
        assert 'id="platform-ai-default-model-help"' in page.text
        assert "Temperature" in page.text
        assert 'id="platform-ai-temperature-help"' in page.text
        assert "Dashboard insights default enabled" in page.text
        assert "Assistant requests per user per hour" in page.text
        assert "Assistant requests per tenant per hour" in page.text
        assert "Dashboard insights minimum refresh (seconds)" in page.text
        assert "Dashboard insights max per tenant per hour" in page.text
        assert "Extra global instructions" in page.text
        assert 'id="platform-ai-reset-help"' in page.text

        csrf = _prime_csrf(admin_client)
        update = admin_client.post(
            "/platform/ai-settings",
            data={
                CSRF_FORM_FIELD: csrf,
                "default_ai_model": "gpt-5",
                "ai_temperature": "0.6",
                "ai_max_output_tokens": "480",
                "ai_dashboard_insights_enabled": "1",
                "ai_dashboard_cache_ttl_seconds": "900",
                "assistant_requests_per_user_per_hour": "18",
                "assistant_requests_per_tenant_per_hour": "120",
                "dashboard_insights_min_refresh_seconds": "420",
                "dashboard_insights_max_per_tenant_per_hour": "16",
                "ai_default_response_style": "balanced",
                "ai_default_focus": "accounts",
                "ai_extra_global_instructions": "Prioritize overdue invoices when relevant.",
            },
            follow_redirects=False,
        )
        assert update.status_code in {302, 303}
        assert update.headers.get("location") == "/platform/ai-settings?saved=1"

        saved_page = admin_client.get(update.headers["location"])
        assert saved_page.status_code == 200
        assert "Platform AI defaults updated." in saved_page.text
        assert 'option value="gpt-5" selected' in saved_page.text
        assert 'value="0.60"' in saved_page.text
        assert 'value="480"' in saved_page.text
        assert 'value="900"' in saved_page.text
        assert 'value="18"' in saved_page.text
        assert 'value="120"' in saved_page.text
        assert 'value="420"' in saved_page.text
        assert 'value="16"' in saved_page.text
        assert "Prioritize overdue invoices when relevant." in saved_page.text

    with SessionLocal() as db:
        row = db.execute(select(PlatformSetting)).scalars().first()
        assert row is not None
        assert row.default_ai_model == "gpt-5"
        assert float(row.ai_temperature or 0) == 0.6
        assert int(row.ai_max_output_tokens or 0) == 480
        assert bool(row.ai_dashboard_insights_enabled) is True
        assert int(row.ai_dashboard_cache_ttl_seconds or 0) == 900
        assert int(row.assistant_requests_per_user_per_hour or 0) == 18
        assert int(row.assistant_requests_per_tenant_per_hour or 0) == 120
        assert int(row.dashboard_insights_min_refresh_seconds or 0) == 420
        assert int(row.dashboard_insights_max_per_tenant_per_hour or 0) == 16
        assert row.ai_default_response_style == "balanced"
        assert row.ai_default_focus == "accounts"
        assert row.ai_extra_global_instructions == "Prioritize overdue invoices when relevant."

        audit_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "PLATFORM_AI_SETTINGS_UPDATE",
                AuditEvent.entity_type == "platform_setting",
                AuditEvent.entity_id == "global",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert audit_event is not None
        assert audit_event.user_id == superadmin_id
        assert isinstance(audit_event.details_json, dict)
        assert audit_event.details_json.get("changed", {}).get("default_ai_model", {}).get("to") == "gpt-5"


def test_platform_superadmin_can_reset_platform_ai_settings_to_defaults(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="platform-ai-settings-reset.db", monkeypatch=monkeypatch
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )
    _save_platform_ai_settings(
        SessionLocal,
        default_ai_model="gpt-5",
        ai_temperature=0.8,
        ai_max_output_tokens=500,
        ai_dashboard_insights_enabled=False,
        ai_dashboard_cache_ttl_seconds=1200,
        ai_default_response_style="detailed",
        ai_default_focus="accounts",
        ai_extra_global_instructions="Use longer operational summaries.",
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(
            admin_client,
            email="superadmin@example.com",
            password="TestPass123!",
            next_path="/platform/tenants",
        ) == 303
        csrf = _prime_csrf(admin_client)
        reset = admin_client.post(
            "/platform/ai-settings/reset",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )
        assert reset.status_code in {302, 303}
        assert reset.headers.get("location") == "/platform/ai-settings?reset=1"

        reset_page = admin_client.get(reset.headers["location"])
        assert reset_page.status_code == 200
        assert "Platform AI defaults reset." in reset_page.text
        assert 'option value="gpt-5-mini" selected' in reset_page.text

    defaults = platform_ai_settings_module.platform_ai_settings_defaults()
    with SessionLocal() as db:
        row = db.execute(select(PlatformSetting)).scalars().first()
        assert row is not None
        assert row.default_ai_model == defaults.default_ai_model
        assert float(row.ai_temperature or 0) == defaults.ai_temperature
        assert int(row.ai_max_output_tokens or 0) == defaults.ai_max_output_tokens
        assert bool(row.ai_dashboard_insights_enabled) is defaults.ai_dashboard_insights_enabled
        assert int(row.ai_dashboard_cache_ttl_seconds or 0) == defaults.ai_dashboard_cache_ttl_seconds
        assert (
            int(row.assistant_requests_per_user_per_hour or 0)
            == defaults.assistant_requests_per_user_per_hour
        )
        assert (
            int(row.assistant_requests_per_tenant_per_hour or 0)
            == defaults.assistant_requests_per_tenant_per_hour
        )
        assert (
            int(row.dashboard_insights_min_refresh_seconds or 0)
            == defaults.dashboard_insights_min_refresh_seconds
        )
        assert (
            int(row.dashboard_insights_max_per_tenant_per_hour or 0)
            == defaults.dashboard_insights_max_per_tenant_per_hour
        )
        assert row.ai_default_response_style == defaults.ai_default_response_style
        assert row.ai_default_focus == defaults.ai_default_focus
        assert row.ai_extra_global_instructions == defaults.ai_extra_global_instructions

        audit_event = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "PLATFORM_AI_SETTINGS_RESET",
                AuditEvent.entity_type == "platform_setting",
                AuditEvent.entity_id == "global",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert audit_event is not None
        assert audit_event.user_id == superadmin_id


def test_platform_superadmin_can_update_email_settings_and_send_test_email(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="platform-email-settings.db", monkeypatch=monkeypatch
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    deliveries: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        text = '{"id":"email_123"}'

        def raise_for_status(self) -> None:
            return None

    def _fake_post(url, *, headers, json, timeout):
        deliveries["url"] = url
        deliveries["headers"] = headers
        deliveries["json"] = json
        deliveries["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(email_service_module.httpx, "post", _fake_post)

    with _client(app, base_url="https://admin.localhost") as admin_client:
        _login_platform_superadmin(admin_client)

        page = admin_client.get("/platform/email-settings")
        assert page.status_code == 200
        assert ">Platform Email Settings<" in page.text
        assert 'action="/platform/email-settings"' in page.text
        assert 'action="/platform/email-settings/test"' in page.text
        assert 'id="platform-email-settings-scope-help"' in page.text
        assert 'id="platform-email-api-key-help"' in page.text
        assert 'id="platform-email-test-help"' in page.text

        update = _post_with_csrf(
            admin_client,
            "/platform/email-settings",
            data={
                "email_provider": "resend",
                "resend_api_key": "re_test_api_key",
                "from_email": "platform@example.com",
                "from_display_name": "Weighbridge Platform",
                "reply_to": "reply@example.com",
            },
        )
        assert update.status_code == 303
        assert update.headers.get("location") == "/platform/email-settings?saved=1"

        saved_page = admin_client.get(update.headers["location"])
        assert saved_page.status_code == 200
        assert "Platform email settings updated." in saved_page.text
        assert 'value="resend"' in saved_page.text
        assert 'value="platform@example.com"' in saved_page.text
        assert 'value="Weighbridge Platform"' in saved_page.text
        assert 'value="reply@example.com"' in saved_page.text
        assert 'value="re_test_api_key"' not in saved_page.text

        test_send = _post_with_csrf(
            admin_client,
            "/platform/email-settings/test",
            data={"test_to": "ops@example.com"},
        )
        assert test_send.status_code == 303
        assert (
            test_send.headers.get("location")
            == "/platform/email-settings?test_sent=1&test_to=ops%40example.com"
        )

        sent_page = admin_client.get(test_send.headers["location"])
        assert sent_page.status_code == 200
        assert "Test email sent to ops@example.com." in sent_page.text

    with SessionLocal() as db:
        row = db.execute(select(PlatformSetting)).scalars().first()
        assert row is not None
        assert row.email_provider == "resend"
        assert row.resend_api_key == "re_test_api_key"
        assert row.from_email == "platform@example.com"
        assert row.from_display_name == "Weighbridge Platform"
        assert row.reply_to == "reply@example.com"

        update_audit = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "PLATFORM_EMAIL_SETTINGS_UPDATE",
                AuditEvent.entity_type == "platform_setting",
                AuditEvent.entity_id == "global",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert update_audit is not None
        assert update_audit.user_id == superadmin_id
        assert (
            update_audit.details_json.get("changed", {}).get("from_email", {}).get("to")
            == "platform@example.com"
        )
        assert (
            update_audit.details_json.get("changed", {}).get("resend_api_key_configured", {}).get("to")
            is True
        )

        test_audit = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "PLATFORM_TEST_EMAIL",
                AuditEvent.entity_type == "platform_setting",
                AuditEvent.entity_id == "global",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert test_audit is not None
        assert test_audit.user_id == superadmin_id
        assert test_audit.details_json.get("status") == "sent"
        assert test_audit.details_json.get("test_to") == "ops@example.com"

    assert deliveries.get("url") == email_service_module.RESEND_SEND_EMAIL_URL
    assert deliveries.get("headers") == {"Authorization": "Bearer re_test_api_key"}
    assert deliveries.get("timeout") == email_service_module.DEFAULT_RESEND_TIMEOUT_SECONDS
    payload = deliveries.get("json")
    assert payload is not None
    assert payload["from"] == "Weighbridge Platform <platform@example.com>"
    assert payload["reply_to"] == "reply@example.com"
    assert payload["to"] == ["ops@example.com"]
    assert payload["subject"] == "Weighbridge Web test email"
    assert "platform email settings page" in payload["text"]


def test_platform_test_email_failure_is_reported_and_audited(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="platform-email-test-failure.db", monkeypatch=monkeypatch
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        _login_platform_superadmin(admin_client)

        failure = _post_with_csrf(
            admin_client,
            "/platform/email-settings/test",
            data={"test_to": "ops@example.com"},
        )
        assert failure.status_code == 303
        assert failure.headers.get("location") == (
            "/platform/email-settings?test_error=Resend+API+key+is+not+configured.&test_to=ops%40example.com"
        )

        failed_page = admin_client.get(failure.headers["location"])
        assert failed_page.status_code == 200
        assert "Resend API key is not configured." in failed_page.text

    with SessionLocal() as db:
        test_audit = db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "PLATFORM_TEST_EMAIL",
                AuditEvent.entity_type == "platform_setting",
                AuditEvent.entity_id == "global",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        ).scalars().first()
        assert test_audit is not None
        assert test_audit.user_id == superadmin_id
        assert test_audit.details_json.get("status") == "failed"
        assert test_audit.details_json.get("error") == "Resend API key is not configured."


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
    demo_admin_id = _seed_user(
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
        assert "Primary Tenant Admin" in detail.text
        assert "Current primary admin" in detail.text
        assert "Update email" in detail.text
        assert "Reset password" in detail.text
        assert "Primary admin email" in detail.text
        assert "New password" in detail.text

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
        _login_platform_superadmin(admin_client)

        detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert detail.status_code == 200
        assert "No tenant users." in detail.text
        assert f'action="/platform/tenants/{demo_tenant}/users"' in detail.text
        assert "Primary Tenant Admin" in detail.text
        assert "Add User" in detail.text
        assert 'id="platform-tenant-users-help"' in detail.text
        assert 'id="platform-add-tenant-user-help"' in detail.text
        assert 'id="platform-add-user-role-help"' in detail.text
        assert "The first active user for this tenant must be a Tenant Admin." in detail.text
        assert "Full control of the tenant workspace, users, settings, and operational/admin functions." in detail.text
        assert "Day-to-day weighbridge and ticket operations" in detail.text
        assert "Invoicing, payments, and finance/admin tasks" in detail.text
        assert "View-only access. No changes are allowed." in detail.text

        created_admin = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{demo_tenant}/users",
            data={
                "first_name": "Demi",
                "last_name": "Admin",
                "email": "demo-admin@example.com",
                "role": ROLE_TENANT_ADMIN,
                "password": "DemoPass123!",
                "confirm_password": "DemoPass123!",
            },
        )
        assert created_admin.status_code in {302, 303}
        assert created_admin.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        created_admin_page = admin_client.get(created_admin.headers["location"])
        assert created_admin_page.status_code == 200
        assert "Tenant user created: demo-admin@example.com." in created_admin_page.text
        assert "Demi Admin" in created_admin_page.text
        assert "demo-admin@example.com" in created_admin_page.text
        assert "Primary Tenant Admin" in created_admin_page.text
        assert "Current primary admin" in created_admin_page.text
        assert "Update email" in created_admin_page.text
        assert "Reset password" in created_admin_page.text
        assert "Full name" in created_admin_page.text
        assert "Email" in created_admin_page.text
        assert "Role" in created_admin_page.text
        assert "Status" in created_admin_page.text
        assert "Created" in created_admin_page.text

        created_operator = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{demo_tenant}/users",
            data={
                "first_name": "Opal",
                "last_name": "Operator",
                "email": "demo-ops@example.com",
                "role": ROLE_USER,
                "password": "OperatorPass123!",
                "confirm_password": "OperatorPass123!",
            },
        )
        assert created_operator.status_code in {302, 303}
        assert created_operator.headers.get("location", "").startswith(f"/platform/tenants/{demo_tenant}?")

        created_operator_page = admin_client.get(created_operator.headers["location"])
        assert created_operator_page.status_code == 200
        assert "Tenant user created: demo-ops@example.com." in created_operator_page.text
        assert "Opal Operator" in created_operator_page.text
        assert "demo-admin@example.com" in created_operator_page.text
        assert "demo-ops@example.com" in created_operator_page.text
        assert "Operator" in created_operator_page.text

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
        assert [user.email for user in demo_users] == [
            "demo-admin@example.com",
            "demo-ops@example.com",
        ]
        assert [user.full_name for user in demo_users] == [
            "Demi Admin",
            "Opal Operator",
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
        created_details = {
            str((event.details_json or {}).get("email") or ""): event.details_json or {}
            for event in created_events
        }
        assert created_details["demo-admin@example.com"]["first_name"] == "Demi"
        assert created_details["demo-admin@example.com"]["last_name"] == "Admin"
        assert created_details["demo-admin@example.com"]["role"] == ROLE_TENANT_ADMIN
        assert created_details["demo-ops@example.com"]["first_name"] == "Opal"
        assert created_details["demo-ops@example.com"]["last_name"] == "Operator"
        assert created_details["demo-ops@example.com"]["role"] == ROLE_USER


def test_platform_superadmin_can_edit_disable_and_reset_tenant_users_from_tenant_detail(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-detail-user-manage.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="tenant-a", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#305a7a",
    )
    _seed_user(
        SessionLocal,
        email="primary-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
        first_name="Priya",
        last_name="Admin",
    )
    managed_user_id = _seed_user(
        SessionLocal,
        email="operator@example.com",
        password="OperatorPass123!",
        role=ROLE_OPERATOR,
        tenant_id=tenant_id,
        first_name="Opal",
        last_name="Operator",
    )
    superadmin_id = _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with _client(app, base_url="https://admin.localhost") as admin_client:
        _login_platform_superadmin(admin_client)

        detail = admin_client.get(f"/platform/tenants/{tenant_id}")
        assert detail.status_code == 200
        assert "Actions" in detail.text
        assert f"/platform/tenants/{tenant_id}?edit_user={managed_user_id}" in detail.text
        assert f'action="/platform/tenants/{tenant_id}/users/{managed_user_id}/toggle-active"' in detail.text
        assert f"/platform/tenants/{tenant_id}?reset_user={managed_user_id}" in detail.text
        assert "Managed above" in detail.text

        edit_page = admin_client.get(f"/platform/tenants/{tenant_id}?edit_user={managed_user_id}")
        assert edit_page.status_code == 200
        assert f'id="edit_user_{managed_user_id}_first_name"' in edit_page.text
        assert f'id="edit_user_{managed_user_id}_last_name"' in edit_page.text
        assert f'id="edit_user_{managed_user_id}_role"' in edit_page.text
        assert "Edit User" in edit_page.text

        update_response = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{tenant_id}/users/{managed_user_id}/update",
            data={
                "first_name": "Avery",
                "last_name": "Accounts",
                "role": ROLE_ACCOUNTS,
            },
        )
        assert update_response.status_code in {302, 303}
        assert update_response.headers.get("location") == f"/platform/tenants/{tenant_id}?user_message=User+updated."

        updated_page = admin_client.get(update_response.headers["location"])
        assert updated_page.status_code == 200
        assert "User updated." in updated_page.text
        assert "Avery Accounts" in updated_page.text
        assert "Accounts" in updated_page.text

        disable_response = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{tenant_id}/users/{managed_user_id}/toggle-active",
        )
        assert disable_response.status_code in {302, 303}
        assert disable_response.headers.get("location") == f"/platform/tenants/{tenant_id}?user_message=User+disabled."

    with _client(app, base_url="https://tenant-a.localhost") as tenant_client:
        assert _login(tenant_client, email="operator@example.com", password="OperatorPass123!") == 401

    with _client(app, base_url="https://admin.localhost") as admin_client:
        _login_platform_superadmin(admin_client)

        enable_response = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{tenant_id}/users/{managed_user_id}/toggle-active",
        )
        assert enable_response.status_code in {302, 303}
        assert enable_response.headers.get("location") == f"/platform/tenants/{tenant_id}?user_message=User+enabled."

        reset_page = admin_client.get(f"/platform/tenants/{tenant_id}?reset_user={managed_user_id}")
        assert reset_page.status_code == 200
        assert f'id="reset_user_{managed_user_id}_password"' in reset_page.text
        assert f'id="reset_user_{managed_user_id}_confirm_password"' in reset_page.text

        reset_response = _post_with_csrf(
            admin_client,
            f"/platform/tenants/{tenant_id}/users/{managed_user_id}/reset-password",
            data={
                "password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
            },
        )
        assert reset_response.status_code in {302, 303}
        assert reset_response.headers.get("location") == f"/platform/tenants/{tenant_id}?user_message=Password+updated."

        reset_follow = admin_client.get(reset_response.headers["location"])
        assert reset_follow.status_code == 200
        assert "Password updated." in reset_follow.text

    with _client(app, base_url="https://tenant-a.localhost") as tenant_client:
        assert _login(tenant_client, email="operator@example.com", password="OperatorPass123!") == 401
        assert _login(tenant_client, email="operator@example.com", password="ResetPass123!") == 303

    with SessionLocal() as db:
        refreshed = db.get(User, managed_user_id)
        assert refreshed is not None
        assert refreshed.first_name == "Avery"
        assert refreshed.last_name == "Accounts"
        assert refreshed.role == ROLE_ACCOUNTS
        assert refreshed.is_active is True

        actions = {
            row.action
            for row in db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "user",
                    AuditEvent.entity_id == str(managed_user_id),
                    AuditEvent.user_id == superadmin_id,
                )
            ).scalars()
        }
        assert "USER_ROLE_CHANGE" in actions
        assert "USER_UPDATE" in actions
        assert "USER_DEACTIVATE" in actions
        assert "USER_ACTIVATE" in actions
        assert "USER_PASSWORD_RESET" in actions


def test_platform_tenant_detail_renders_section_structure_and_name_fallbacks(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-detail-users-render.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="primary-admin@example.com",
        password="TenantPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
        first_name="Priya",
        last_name="Admin",
    )
    _seed_user(
        SessionLocal,
        email="legacy-user@example.com",
        password="TenantPass123!",
        role=ROLE_USER,
        tenant_id=tenant_id,
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
        detail = admin_client.get(f"/platform/tenants/{tenant_id}")

    assert detail.status_code == 200
    section_positions = [
        detail.text.index("<h2>Tenant Summary"),
        detail.text.index("<h2>Primary Tenant Admin"),
        detail.text.index("<h2>Users"),
        detail.text.index("<h2>Add User"),
        detail.text.index("<h2>Tenant AI Overrides"),
        detail.text.index("<h2>Maintenance"),
    ]
    assert section_positions == sorted(section_positions)
    assert "Current primary admin" in detail.text
    assert "Update email" in detail.text
    assert "Reset password" in detail.text
    assert "Full name" in detail.text
    assert "Email" in detail.text
    assert "Role" in detail.text
    assert "Status" in detail.text
    assert "Created" in detail.text
    assert "Actions" in detail.text
    assert "Priya Admin" in detail.text
    assert "primary-admin@example.com" in detail.text
    assert detail.text.count("legacy-user@example.com") >= 2
    assert "Add User" in detail.text
    assert "Tenant Admin" in detail.text
    assert "Operator" in detail.text
    assert "Accounts" in detail.text
    assert "Read Only" in detail.text
    add_user_start = detail.text.index("<h2>Add User")
    ai_overrides_start = detail.text.index("<h2>Tenant AI Overrides")
    add_user_section = detail.text[add_user_start:ai_overrides_start]
    field_positions = [
        add_user_section.index('id="first_name"'),
        add_user_section.index('id="last_name"'),
        add_user_section.index('id="email"'),
        add_user_section.index('id="role"'),
        add_user_section.index('id="password"'),
        add_user_section.index('id="user_confirm_password"'),
    ]
    assert field_positions == sorted(field_positions)


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
    demo_admin_id = _seed_user(
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
        assert 'Type "DEMO" to confirm reset' in demo_detail.text
        assert f'action="/platform/tenants/{demo_tenant}/reset-demo"' in demo_detail.text
        assert f'action="/platform/tenants/{demo_tenant}/demo-reset-schedule"' in demo_detail.text
        assert "demo@demo.com" in demo_detail.text
        assert "password" in demo_detail.text

        marked_demo_detail = admin_client.get(f"/platform/tenants/{marked_demo_tenant}")
        assert marked_demo_detail.status_code == 200
        assert f'action="/platform/tenants/{marked_demo_tenant}/reset-demo"' in marked_demo_detail.text
        assert f'action="/platform/tenants/{marked_demo_tenant}/demo-reset-schedule"' not in marked_demo_detail.text
        assert (
            "Delete is blocked for the demo tenant because it is reserved for internal demo/testing use."
            in marked_demo_detail.text
        )
        assert "Automatic demo reset is only available for the reserved demo workspace." in marked_demo_detail.text

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

        blocked_schedule = admin_client.post(
            f"/platform/tenants/{marked_demo_tenant}/demo-reset-schedule",
            data={
                CSRF_FORM_FIELD: csrf,
                "demo_reset_interval_days": "7",
                "demo_reset_time": "03:00",
            },
            follow_redirects=False,
        )
        assert blocked_schedule.status_code in {302, 303}
        assert blocked_schedule.headers.get("location", "").startswith(
            f"/platform/tenants/{marked_demo_tenant}?"
        )

        blocked_schedule_page = admin_client.get(blocked_schedule.headers["location"])
        assert blocked_schedule_page.status_code == 200
        assert "Automatic demo reset is only available for the reserved demo workspace." in blocked_schedule_page.text

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

    demo_admin_id = _seed_user(
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

    def assert_demo_dataset_counts(db) -> None:
        expected_demo_product_ewc = {
            "MIXEDW": ("170904", False, "Riverside Landfill"),
            "CLAYSOIL": ("170503", True, "Hazardous Bay 1"),
            "WOODW": ("170201", False, "Wood/Timber Bay 1"),
            "SKIP8": ("150106", False, "Riverside Landfill"),
            "BALES": ("150106", False, "Wood/Timber Bay 1"),
        }
        expected_demo_ewc_rows = {
            "150106": ("Mixed packaging", False),
            "170201": ("Wood", False),
            "170503": ("Soil and stones containing hazardous substances", True),
            "170904": ("Mixed construction and demolition waste", False),
        }
        assert _tenant_row_count(db, Customer, demo_tenant) == 25
        assert _tenant_row_count(db, Vehicle, demo_tenant) == 16
        assert _tenant_row_count(db, Product, demo_tenant) == 15
        assert _tenant_row_count(db, Container, demo_tenant) == 4
        assert _tenant_row_count(db, Driver, demo_tenant) == 9
        assert _tenant_row_count(db, Haulier, demo_tenant) == 6
        assert _tenant_row_count(db, Destination, demo_tenant) == 6
        assert _tenant_row_count(db, Ticket, demo_tenant) == 32
        assert _tenant_row_count(db, Invoice, demo_tenant) == 7
        assert _tenant_row_count(db, InvoiceLine, demo_tenant) == 19
        assert _tenant_row_count(db, User, demo_tenant) == 1
        assert {
            value
            for value in db.execute(
                select(Destination.name).where(Destination.tenant_id == demo_tenant)
            ).scalars()
        } == {
            "Hazardous Bay 1",
            "Hazardous Bay 2",
            "Inert Waste Bay 1",
            "Inert Waste Bay 2",
            "Riverside Landfill",
            "Wood/Timber Bay 1",
        }
        assert db.execute(select(func.count(EwcCode.id))).scalar_one() == 4
        demo_vehicle_profiles = db.execute(
            select(
                Vehicle.registration,
                Vehicle.default_tare_kg,
                Vehicle.overweight_threshold_kg,
                VehicleType.code,
            )
            .join(VehicleType, Vehicle.vehicle_type_id == VehicleType.id)
            .where(Vehicle.tenant_id == demo_tenant)
        ).all()
        assert len(demo_vehicle_profiles) == 16
        thresholds_by_type: dict[str, set[int]] = {}
        for _, tare_kg, threshold_kg, vehicle_type_code in demo_vehicle_profiles:
            assert tare_kg is not None
            assert threshold_kg is not None
            assert Decimal(str(threshold_kg)) > Decimal(str(tare_kg))
            thresholds_by_type.setdefault(str(vehicle_type_code), set()).add(int(Decimal(str(threshold_kg))))
        assert all(28000 not in values for values in thresholds_by_type.values())
        assert thresholds_by_type["Van"] == {3000, 3500}
        assert thresholds_by_type["6 Wheeler"] == {24000, 26000}
        assert thresholds_by_type["8 Wheeler"] == {32000}
        assert thresholds_by_type["Artic"] == {40000, 44000}
        assert thresholds_by_type["Tractor & Trailer"] == {38000, 40000}
        assert (
            db.execute(
                select(func.count(Vehicle.id)).where(
                    Vehicle.tenant_id == demo_tenant,
                    Vehicle.default_haulier_id.is_(None),
                )
            ).scalar_one()
            == 6
        )
        assert (
            db.execute(
                select(func.count(Vehicle.id)).where(
                    Vehicle.tenant_id == demo_tenant,
                    Vehicle.default_haulier_id.is_not(None),
                )
            ).scalar_one()
            == 10
        )

        demo_ewc_rows = {
            row.code_6: row
            for row in db.execute(
                select(EwcCode).where(EwcCode.code_6.in_(expected_demo_ewc_rows))
            ).scalars()
        }
        assert set(demo_ewc_rows) == set(expected_demo_ewc_rows)
        for code_6, (description, hazardous) in expected_demo_ewc_rows.items():
            row = demo_ewc_rows[code_6]
            assert row.code_display == f"{code_6[0:2]} {code_6[2:4]} {code_6[4:6]}"
            assert row.description == description
            assert bool(row.hazardous) is hazardous
            assert bool(row.active) is True

        demo_waste_products = {
            row.code: row
            for row in db.execute(
                select(Product).where(
                    Product.tenant_id == demo_tenant,
                    Product.code.in_(expected_demo_product_ewc),
                )
            ).scalars()
        }
        assert set(demo_waste_products) == set(expected_demo_product_ewc)
        zero_rated_demo_products = {
            code: rate_percent
            for code, rate_percent in db.execute(
                select(Product.code, TaxRate.rate_percent)
                .join(TaxRate, Product.tax_rate_id == TaxRate.id)
                .where(
                    Product.tenant_id == demo_tenant,
                    Product.code.in_(("EARDEF", "HIVIS")),
                )
            ).all()
        }
        assert zero_rated_demo_products == {
            "EARDEF": Decimal("0.00"),
            "HIVIS": Decimal("0.00"),
        }
        for code, (ewc_code_6, hazardous, destination_name) in expected_demo_product_ewc.items():
            product = demo_waste_products[code]
            assert product.ewc_code is not None
            assert product.ewc_code.code_6 == ewc_code_6
            assert bool(product.is_hazardous) is hazardous
            destination = db.get(Destination, product.default_destination_id)
            assert destination is not None
            assert destination.name == destination_name
        assert {
            value
            for value in db.execute(
                select(Customer.invoice_frequency).where(Customer.tenant_id == demo_tenant)
            ).scalars()
        } == {None, "WEEKLY", "MONTHLY", "ADHOC"}
        assert {
            value
            for value in db.execute(
                select(Customer.payment_terms_days).where(Customer.tenant_id == demo_tenant)
            ).scalars()
        } == {None, 7, 14, 30}
        assert db.execute(
            select(func.count(Customer.id)).where(
                Customer.tenant_id == demo_tenant,
                Customer.payment_terms_days.is_(None),
            )
        ).scalar_one() > 0
        assert db.execute(
            select(func.count(Customer.id)).where(
                Customer.tenant_id == demo_tenant,
                Customer.payment_terms == "Ad Hoc",
            )
        ).scalar_one() > 0
        assert (
            db.execute(
                select(func.count(Customer.id)).where(
                    Customer.tenant_id == demo_tenant,
                    Customer.on_stop.is_(True),
                )
            ).scalar_one()
            == 2
        )
        assert db.execute(
            select(func.count(Customer.id)).where(
                Customer.tenant_id == demo_tenant,
                Customer.do_not_invoice.is_(True),
            )
        ).scalar_one() > 0
        assert db.execute(
            select(func.count(Customer.id)).where(
                Customer.tenant_id == demo_tenant,
                Customer.must_have_po.is_(True),
            )
        ).scalar_one() > 0
        assert db.execute(
            select(func.count(Customer.id)).where(
                Customer.tenant_id == demo_tenant,
                Customer.is_cash_account.is_(True),
            )
        ).scalar_one() > 0
        assert (
            db.execute(
                select(func.count(Customer.id)).where(
                    Customer.tenant_id == demo_tenant,
                    Customer.do_not_invoice.is_(True),
                    Customer.must_have_po.is_(True),
                )
            ).scalar_one()
            == 2
        )
        assert (
            db.execute(
                select(func.count(Customer.id)).where(
                    Customer.tenant_id == demo_tenant,
                    Customer.account_code.like("CUST%"),
                )
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                select(func.count(CustomerProductPrice.id)).where(
                    CustomerProductPrice.tenant_id == demo_tenant,
                    CustomerProductPrice.is_active.is_(True),
                )
            ).scalar_one()
            == 5
        )
        assert (
            db.execute(
                select(func.count(func.distinct(CustomerProductPrice.customer_id))).where(
                    CustomerProductPrice.tenant_id == demo_tenant,
                    CustomerProductPrice.is_active.is_(True),
                )
            ).scalar_one()
            == 5
        )
        demo_customers = db.execute(
            select(Customer).where(Customer.tenant_id == demo_tenant)
        ).scalars().all()
        customers_with_outstanding = [
            customer
            for customer in demo_customers
            if customer_outstanding_total(db, customer.id) > Decimal("0.00")
        ]
        assert len(customers_with_outstanding) == 5
        assert any(
            bool(customer.on_stop)
            and customer_outstanding_total(db, customer.id) > (customer.credit_limit or Decimal("0.00"))
            for customer in demo_customers
        )
        assert (
            db.execute(
                select(func.count(Ticket.id)).where(
                    Ticket.tenant_id == demo_tenant,
                    Ticket.status == TicketStatusEnum.OPEN.value,
                )
            ).scalar_one()
            == 7
        )
        assert (
            db.execute(
                select(func.count(Ticket.id)).where(
                    Ticket.tenant_id == demo_tenant,
                    Ticket.status == TicketStatusEnum.COMPLETE.value,
                )
            ).scalar_one()
            == 25
        )
        assert (
            db.execute(
                select(func.count(Ticket.id)).where(
                    Ticket.tenant_id == demo_tenant,
                    Ticket.transaction_type.in_(
                        [
                            TransactionTypeEnum.WASTEIN.value,
                            TransactionTypeEnum.WASTEOUT.value,
                        ]
                    ),
                )
            ).scalar_one()
            == 8
        )
        completed_demo_waste_tickets = db.execute(
            select(Ticket).where(
                Ticket.tenant_id == demo_tenant,
                Ticket.status == TicketStatusEnum.COMPLETE.value,
                Ticket.transaction_type.in_(
                    [
                        TransactionTypeEnum.WASTEIN.value,
                        TransactionTypeEnum.WASTEOUT.value,
                    ]
                ),
            )
        ).scalars().all()
        assert sum(
            1 for ticket in completed_demo_waste_tickets if ticket.wtn_signature_status == "signed"
        ) == 2
        assert sum(
            1 for ticket in completed_demo_waste_tickets if ticket.wtn_signature_status == "partial"
        ) == 3
        assert sum(
            1 for ticket in completed_demo_waste_tickets if ticket.wtn_signature_status == "unsigned"
        ) == 1
        assert any(
            ticket.has_wtn_receiver_signature
            and not ticket.has_wtn_carrier_signature
            and not ticket.has_wtn_producer_signature
            for ticket in completed_demo_waste_tickets
        )
        assert any(
            ticket.has_wtn_receiver_signature
            and ticket.has_wtn_carrier_signature
            and not ticket.has_wtn_producer_signature
            for ticket in completed_demo_waste_tickets
        )
        assert any(
            ticket.has_wtn_carrier_signature
            and not ticket.has_wtn_receiver_signature
            and not ticket.has_wtn_producer_signature
            for ticket in completed_demo_waste_tickets
        )
        demo_invoice_prefix = f"INV-{str(utcnow().year)[2:]}"
        assert list(
            db.execute(
                select(Invoice.invoice_no)
                .where(Invoice.tenant_id == demo_tenant)
                .order_by(Invoice.invoice_no.asc())
            ).scalars()
        ) == [
            f"{demo_invoice_prefix}-{number:05d}"
            for number in range(1, 8)
        ]
        invoice_line_counts = db.execute(
            select(InvoiceLine.invoice_id, func.count(InvoiceLine.id))
            .where(InvoiceLine.tenant_id == demo_tenant)
            .group_by(InvoiceLine.invoice_id)
        ).all()
        assert any(int(line_count) > 1 for _, line_count in invoice_line_counts)

    with SessionLocal() as db:
        db.add(Customer(tenant_id=demo_tenant, account_code="DEMO-001", name="Demo Customer"))
        db.add(Vehicle(tenant_id=demo_tenant, registration="DEMO123"))
        db.add(Customer(tenant_id=other_tenant, account_code="OTHER-001", name="Other Customer"))
        db.add(Vehicle(tenant_id=other_tenant, registration="OTHER123"))
        db.commit()

    with SessionLocal() as db:
        assert db.execute(select(func.count(EwcCode.id))).scalar_one() == 0
        template = (
            db.execute(
                select(PrintTemplate)
                .where(
                    PrintTemplate.tenant_id == demo_tenant,
                    PrintTemplate.document_type == "TICKET",
                )
                .order_by(PrintTemplate.id.asc())
            ).scalars().first()
        )
        assert template is not None
        agent = PrintAgent(
            id="demo-reset-agent-manual",
            tenant_id=demo_tenant,
            name="Demo Reset Agent",
            api_key="demo-reset-agent-manual-key",
            status="ONLINE",
            printers_json=[{"name": "Demo Reset Printer", "is_default": True, "is_online": True}],
        )
        db.add(agent)
        db.add(
            PrintAgentPairing(
                id="demo-reset-pairing-pending-manual",
                tenant_id=demo_tenant,
                requested_name="Demo Reset Pending Pairing",
                paired_name=None,
                pairing_code_hash="p" * 64,
                exchange_token_hash="q" * 64,
                status="PENDING",
                expires_at=utcnow() + timedelta(days=1),
                paired_at=None,
                paired_by_user_id=None,
                exchanged_at=None,
                print_agent_id=None,
            )
        )
        db.add(
            PrintAgentPairing(
                id="demo-reset-pairing-manual",
                tenant_id=demo_tenant,
                requested_name="Demo Reset Agent",
                paired_name="Demo Reset Agent",
                pairing_code_hash="a" * 64,
                exchange_token_hash="b" * 64,
                status="PAIRED",
                expires_at=utcnow() + timedelta(days=1),
                paired_at=utcnow(),
                paired_by_user_id=demo_admin_id,
                exchanged_at=utcnow(),
                print_agent_id=agent.id,
            )
        )
        pull_destination = PrintDestination(
            tenant_id=demo_tenant,
            name="Demo Reset Pull Destination",
            description="Stale demo pull destination",
            document_type="TICKET",
            template_id=template.id,
            delivery_type="PRINT_AGENT_PULL",
            delivery_config={
                "agent_id": agent.id,
                "printer_name": "Demo Reset Printer",
                "copies": 1,
            },
            is_default=False,
            is_active=True,
        )
        db.add(pull_destination)
        db.flush()
        db.add(
            PrintJob(
                tenant_id=demo_tenant,
                created_by_user_id=demo_admin_id,
                document_type="TICKET",
                destination_id=pull_destination.id,
                template_id=template.id,
                agent_id=agent.id,
                delivery_type="PRINT_AGENT_PULL",
                delivery_config_json={
                    "agent_id": agent.id,
                    "printer_name": "Demo Reset Printer",
                    "copies": 1,
                },
                rendered_content="Demo reset stale pull job",
                payload_format="TEXT",
                payload_mime_type="text/plain",
                trigger_source="MANUAL",
                status="PENDING",
            )
        )
        db.commit()

    demo_logo_dir = uploads_root / "tenants" / str(demo_tenant) / "company"
    demo_logo_file = demo_logo_dir / "demo-logo.png"
    stale_logo_file = demo_logo_dir / "logo.png"
    demo_logo_dir.mkdir(parents=True, exist_ok=True)
    stale_logo_file.write_bytes(b"demo-logo")

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
        assert "demo@demo.com" in reset_page.text
        assert "password" in reset_page.text

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        assert bool(demo.is_active) is True
        assert bool(demo.is_demo) is True
        assert _tenant_row_count(db, PrintAgent, demo_tenant) == 0
        assert _tenant_row_count(db, PrintAgentPairing, demo_tenant) == 0
        assert _tenant_row_count(db, PrintJob, demo_tenant) == 0
        assert (
            db.execute(
                select(func.count(PrintDestination.id)).where(
                    PrintDestination.tenant_id == demo_tenant,
                    PrintDestination.delivery_type == "PRINT_AGENT_PULL",
                )
            ).scalar_one()
            == 0
        )

        demo_user = db.execute(
            select(User).where(
                User.tenant_id == demo_tenant,
                getattr(User, "email", getattr(User, "username")) == "demo@demo.com",
            )
        ).scalars().first()
        assert demo_user is not None
        assert str(demo_user.role or "").strip().lower() == ROLE_TENANT_ADMIN
        assert bool(demo_user.is_active) is True
        assert_demo_dataset_counts(db)

        company = (
            db.execute(select(CompanySetting).where(CompanySetting.tenant_id == demo_tenant))
            .scalars()
            .first()
        )
        assert company is not None
        assert company.name == "Demo Ltd."
        assert company.address_line1 == "1 Chapter House Street"
        assert company.city == "York"
        assert company.postcode == "YO1 7JH"
        assert company.country == "United Kingdom"
        assert company.navbar_color_hex == "#242B3B"
        assert company.primary_color_hex == "#2596BE"
        assert company.company_logo_path == "/static/uploads/company/demo-logo.png"
        assert company.company_logo_updated_at is not None
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
        ticket_sequence = db.execute(
            select(TicketSequence).where(
                TicketSequence.tenant_id == demo_tenant,
                TicketSequence.year == int(utcnow().year),
            )
        ).scalars().first()
        assert ticket_sequence is not None
        assert ticket_sequence.last_number == 32
        invoice_sequence = db.execute(
            select(InvoiceSequence).where(InvoiceSequence.year == int(utcnow().year))
        ).scalars().first()
        assert invoice_sequence is not None
        assert invoice_sequence.last_number == 7

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
        assert isinstance(reset_event.details_json, dict)
        assert reset_event.details_json.get("default_demo_user_created") is True
        assert reset_event.details_json.get("dataset", {}).get("customers") == 25
        assert reset_event.details_json.get("dataset", {}).get("tickets") == 32
        assert reset_event.details_json.get("dataset", {}).get("tickets_open") == 7
        assert reset_event.details_json.get("dataset", {}).get("tickets_complete") == 25
        assert reset_event.details_json.get("dataset", {}).get("tickets_waste") == 8
        assert reset_event.details_json.get("dataset", {}).get("invoices") == 7
        assert reset_event.details_json.get("dataset", {}).get("ewc_codes") == 4

    assert demo_logo_file.is_file()
    assert stale_logo_file.exists() is False

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="demo@demo.com", password="password") == 303
        print_agents_page = demo_client.get("/admin/printing/agents")
        assert print_agents_page.status_code == 200
        assert "No paired agents yet." in print_agents_page.text
        assert "No pending pairings." in print_agents_page.text
        assert "Demo Reset Agent" not in print_agents_page.text
        assert "Demo Reset Pending Pairing" not in print_agents_page.text

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        created_user = admin_client.post(
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
        assert created_user.status_code in {302, 303}

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="demo-admin@example.com", password="DemoPass123!") == 303
        demo_ticket_prefix = str(utcnow().year)[2:]
        demo_invoice_prefix = f"INV-{demo_ticket_prefix}"

        dashboard = demo_client.get("/")
        assert dashboard.status_code == 200
        assert "dashboard-empty-state" not in dashboard.text
        assert "Demo Ltd." in dashboard.text
        assert "/static/uploads/company/demo-logo.png?v=" in dashboard.text
        assert _dashboard_metric_value(dashboard.text, "open_tickets") == "7"
        assert _dashboard_metric_value(dashboard.text, "invoices_pending") == "2"
        assert "INV-DEMO" not in dashboard.text

        branding_css = demo_client.get("/branding.css")
        assert branding_css.status_code == 200
        assert "--nav-bg: #242B3B;" in branding_css.text
        assert "--primary: #2596BE;" in branding_css.text

        logo_response = demo_client.get("/static/uploads/company/demo-logo.png")
        assert logo_response.status_code == 200
        assert logo_response.content

        tickets_page = demo_client.get("/tickets")
        assert tickets_page.status_code == 200
        assert f"{demo_ticket_prefix}-00032" in tickets_page.text

        customers_page = demo_client.get("/customers")
        assert customers_page.status_code == 200
        assert "Beacon Aggregates Ltd" in customers_page.text
        assert "Meadow Industrial Park" in customers_page.text
        assert "David Gregson" in customers_page.text
        assert "Claire Bennett" in customers_page.text
        assert "DGREGSON" in customers_page.text
        assert "CBENNETT" in customers_page.text
        assert "NET 7" in customers_page.text
        assert "NET 14" in customers_page.text
        assert "NET 30" in customers_page.text
        assert "Ad Hoc" in customers_page.text
        assert "Yes (" in customers_page.text
        assert customers_page.text.count('class="pricing-indicator"') == 5

        vehicles_page = demo_client.get("/vehicles")
        assert vehicles_page.status_code == 200
        assert "YP24KDM" in vehicles_page.text
        assert "NX72KLU" in vehicles_page.text
        assert "3,500 kg" in vehicles_page.text
        assert "44,000 kg" in vehicles_page.text
        assert "28,000 kg" not in vehicles_page.text

        products_page = demo_client.get("/products")
        assert products_page.status_code == 200
        assert "Product type" in products_page.text
        assert "Basis" in products_page.text
        assert "Recycled Aggregate 20mm" in products_page.text
        assert "Screened Topsoil (m3)" in products_page.text
        assert "Ear Defenders" in products_page.text
        assert "High Vis Vest" in products_page.text
        assert "Compacted Bale Removal" in products_page.text
        assert "Weight" in products_page.text
        assert "Count" in products_page.text
        assert "0%" in products_page.text

        products_new = demo_client.get("/products/new")
        assert products_new.status_code == 200
        assert "17 09 04" in products_new.text
        assert "17 05 03" in products_new.text

        product_groups_page = demo_client.get("/products/groups")
        assert product_groups_page.status_code == 200
        assert "Aggregate Sales" in product_groups_page.text
        assert "General Sales" in product_groups_page.text
        assert "4000" in product_groups_page.text

        invoices_page = demo_client.get("/invoices")
        assert invoices_page.status_code == 200
        assert f"{demo_invoice_prefix}-00007" in invoices_page.text
        assert "INV-DEMO" not in invoices_page.text

    with _client(app, base_url="https://admin.localhost") as admin_client:
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(admin_client)
        second_reset = admin_client.post(
            f"/platform/tenants/{demo_tenant}/reset-demo",
            data={
                CSRF_FORM_FIELD: csrf,
                "confirmation_text": "DEMO",
            },
            follow_redirects=False,
        )
        assert second_reset.status_code in {302, 303}

    assert demo_logo_file.is_file()

    with SessionLocal() as db:
        assert _tenant_row_count(db, User, demo_tenant) == 1
        default_demo_user = (
            db.execute(
                select(User).where(
                    User.tenant_id == demo_tenant,
                    getattr(User, "email", getattr(User, "username")) == "demo@demo.com",
                )
            ).scalars().first()
        )
        assert default_demo_user is not None
        assert default_demo_user.saved_signature_data_uri == DEMO_SIGNATURE_DATA_URI
        assert default_demo_user.saved_signature_signer_name == "Demo Admin"
        assert default_demo_user.saved_signature_updated_at is not None
        assert_demo_dataset_counts(db)


def test_reserved_demo_auto_reset_schedule_resets_on_next_request(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-auto-reset.db", monkeypatch=monkeypatch
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
        primary_color="#335577",
    )
    stale_user_id = _seed_user(
        SessionLocal,
        email="stale-demo@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
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
        saved = admin_client.post(
            f"/platform/tenants/{demo_tenant}/demo-reset-schedule",
            data={
                CSRF_FORM_FIELD: csrf,
                "demo_reset_interval_days": "2",
                "demo_reset_time": "04:15",
            },
            follow_redirects=False,
        )
        assert saved.status_code in {302, 303}
        assert saved.headers.get("location") == (
            f"/platform/tenants/{demo_tenant}?demo_reset_schedule_saved=1"
        )

        saved_page = admin_client.get(saved.headers["location"])
        assert saved_page.status_code == 200
        assert "Automatic demo reset schedule updated." in saved_page.text
        assert f'action="/platform/tenants/{demo_tenant}/demo-reset-schedule"' in saved_page.text
        assert 'value="2"' in saved_page.text
        assert 'value="04:15"' in saved_page.text

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        assert demo.demo_reset_interval_days == 2
        assert demo.demo_reset_time_minutes == 255
        assert demo.demo_last_reset_at is not None
        demo.demo_last_reset_at = utcnow() - timedelta(days=3)
        template = (
            db.execute(
                select(PrintTemplate)
                .where(
                    PrintTemplate.tenant_id == demo_tenant,
                    PrintTemplate.document_type == "TICKET",
                )
                .order_by(PrintTemplate.id.asc())
            ).scalars().first()
        )
        assert template is not None
        agent = PrintAgent(
            id="demo-reset-agent-auto",
            tenant_id=demo_tenant,
            name="Auto Reset Agent",
            api_key="demo-reset-agent-auto-key",
            status="ONLINE",
            printers_json=[{"name": "Auto Reset Printer", "is_default": True, "is_online": True}],
        )
        db.add(agent)
        db.add(
            PrintAgentPairing(
                id="demo-reset-pairing-pending-auto",
                tenant_id=demo_tenant,
                requested_name="Auto Reset Pending Pairing",
                paired_name=None,
                pairing_code_hash="e" * 64,
                exchange_token_hash="f" * 64,
                status="PENDING",
                expires_at=utcnow() + timedelta(days=1),
                paired_at=None,
                paired_by_user_id=None,
                exchanged_at=None,
                print_agent_id=None,
            )
        )
        db.add(
            PrintAgentPairing(
                id="demo-reset-pairing-auto",
                tenant_id=demo_tenant,
                requested_name="Auto Reset Agent",
                paired_name="Auto Reset Agent",
                pairing_code_hash="c" * 64,
                exchange_token_hash="d" * 64,
                status="PAIRED",
                expires_at=utcnow() + timedelta(days=1),
                paired_at=utcnow(),
                paired_by_user_id=stale_user_id,
                exchanged_at=utcnow(),
                print_agent_id=agent.id,
            )
        )
        pull_destination = PrintDestination(
            tenant_id=demo_tenant,
            name="Auto Reset Pull Destination",
            description="Auto reset stale pull destination",
            document_type="TICKET",
            template_id=template.id,
            delivery_type="PRINT_AGENT_PULL",
            delivery_config={
                "agent_id": agent.id,
                "printer_name": "Auto Reset Printer",
                "copies": 1,
            },
            is_default=False,
            is_active=True,
        )
        db.add(pull_destination)
        db.flush()
        db.add(
            PrintJob(
                tenant_id=demo_tenant,
                created_by_user_id=stale_user_id,
                document_type="TICKET",
                destination_id=pull_destination.id,
                template_id=template.id,
                agent_id=agent.id,
                delivery_type="PRINT_AGENT_PULL",
                delivery_config_json={
                    "agent_id": agent.id,
                    "printer_name": "Auto Reset Printer",
                    "copies": 1,
                },
                rendered_content="Auto reset stale pull job",
                payload_format="TEXT",
                payload_mime_type="text/plain",
                trigger_source="MANUAL",
                status="PENDING",
            )
        )
        db.add(Customer(tenant_id=demo_tenant, account_code="STALE-001", name="Stale Customer"))
        db.commit()

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        login_page = demo_client.get("/login")
        assert login_page.status_code == 200
        assert "Invalid email or password." not in login_page.text

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        assert demo.demo_last_reset_at is not None
        assert demo.demo_last_reset_at > utcnow() - timedelta(minutes=2)
        assert _tenant_row_count(db, PrintAgent, demo_tenant) == 0
        assert _tenant_row_count(db, PrintAgentPairing, demo_tenant) == 0
        assert _tenant_row_count(db, PrintJob, demo_tenant) == 0
        assert (
            db.execute(
                select(func.count(PrintDestination.id)).where(
                    PrintDestination.tenant_id == demo_tenant,
                    PrintDestination.delivery_type == "PRINT_AGENT_PULL",
                )
            ).scalar_one()
            == 0
        )
        assert _tenant_row_count(db, User, demo_tenant) == 1
        assert db.get(User, stale_user_id) is None
        default_demo_user = (
            db.execute(
                select(User).where(
                    User.tenant_id == demo_tenant,
                    getattr(User, "email", getattr(User, "username")) == "demo@demo.com",
                )
            ).scalars().first()
        )
        assert default_demo_user is not None
        assert default_demo_user.saved_signature_data_uri == DEMO_SIGNATURE_DATA_URI
        assert default_demo_user.saved_signature_signer_name == "Demo Admin"
        assert default_demo_user.saved_signature_updated_at is not None
        seeded_signature = normalize_png_data_url(default_demo_user.saved_signature_data_uri)
        assert seeded_signature is not None
        assert png_has_visible_ink(seeded_signature[1]) is True
        assert (
            db.execute(
                select(Customer).where(
                    Customer.tenant_id == demo_tenant,
                    Customer.account_code == "STALE-001",
                )
            ).scalars().first()
            is None
        )
        assert _tenant_row_count(db, Ticket, demo_tenant) == 32
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
        assert isinstance(reset_event.details_json, dict)
        assert reset_event.details_json.get("reason") == "automatic"
        assert reset_event.details_json.get("default_demo_user_created") is True

    with _client(app, base_url=f"https://{settings.effective_demo_tenant_subdomain}.localhost") as demo_client:
        assert _login(demo_client, email="stale-demo@example.com", password="DemoPass123!") == 401
        assert _login(demo_client, email="demo@demo.com", password="password") == 303
        print_agents_page = demo_client.get("/admin/printing/agents")
        assert print_agents_page.status_code == 200
        assert "No paired agents yet." in print_agents_page.text
        assert "No pending pairings." in print_agents_page.text
        assert "Auto Reset Agent" not in print_agents_page.text
        assert "Auto Reset Pending Pairing" not in print_agents_page.text


def test_reserved_demo_auto_reset_schedule_also_runs_during_platform_requests(tmp_path, monkeypatch):
    reset_now = datetime(2026, 4, 1, 12, 0, 0)
    monkeypatch.setattr("app.services.demo_tenant_reset.utcnow", lambda: reset_now)

    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-auto-reset-platform.db", monkeypatch=monkeypatch
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
        primary_color="#335577",
    )
    stale_user_id = _seed_user(
        SessionLocal,
        email="stale-demo@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        demo.demo_reset_interval_days = 1
        demo.demo_reset_time_minutes = 60
        demo.demo_last_reset_at = datetime(2026, 3, 30, 7, 59, 0)
        db.add(Customer(tenant_id=demo_tenant, account_code="STALE-001", name="Stale Customer"))
        db.commit()

    with _client(app, base_url="https://admin.localhost") as admin_client:
        login_page = admin_client.get("/login")
        assert login_page.status_code == 200
        assert _login(admin_client, email="superadmin@example.com", password="TestPass123!") == 303
        detail = admin_client.get(f"/platform/tenants/{demo_tenant}")
        assert detail.status_code == 200

    with SessionLocal() as db:
        demo = db.get(Tenant, demo_tenant)
        assert demo is not None
        assert demo.demo_last_reset_at == reset_now
        assert _tenant_row_count(db, User, demo_tenant) == 1
        assert db.get(User, stale_user_id) is None
        assert (
            db.execute(
                select(Customer).where(
                    Customer.tenant_id == demo_tenant,
                    Customer.account_code == "STALE-001",
                )
            ).scalars().first()
            is None
        )
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
        assert isinstance(reset_event.details_json, dict)
        assert reset_event.details_json.get("reason") == "automatic"


def test_platform_superadmin_reset_demo_deletes_tenant_ai_usage_logs_before_users(
    tmp_path, monkeypatch
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-demo-reset-ai-usage.db", monkeypatch=monkeypatch
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
        primary_color="#6b2d2d",
    )
    other_tenant = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b", is_active=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=other_tenant,
        company_name="Tenant B",
        primary_color="#2a4f74",
    )

    demo_user_id = _seed_user(
        SessionLocal,
        email="demo-admin@example.com",
        password="DemoPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=demo_tenant,
    )
    other_user_id = _seed_user(
        SessionLocal,
        email="tenant-b-admin@example.com",
        password="TenantBPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=other_tenant,
    )
    _seed_user(
        SessionLocal,
        email="superadmin@example.com",
        password="TestPass123!",
        role=ROLE_SUPERADMIN,
        tenant_id=None,
    )

    with SessionLocal() as db:
        db.add_all(
            [
                AIUsageLog(
                    tenant_id=demo_tenant,
                    user_id=demo_user_id,
                    request_type=ai_usage_module.REQUEST_TYPE_ASSISTANT,
                    success=True,
                    counted_toward_limit=True,
                ),
                AIUsageLog(
                    tenant_id=other_tenant,
                    user_id=other_user_id,
                    request_type=ai_usage_module.REQUEST_TYPE_ASSISTANT,
                    success=True,
                    counted_toward_limit=True,
                ),
            ]
        )
        db.commit()
        assert (
            db.execute(select(func.count(AIUsageLog.id)).where(AIUsageLog.tenant_id == demo_tenant)).scalar_one()
            == 1
        )

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

    with SessionLocal() as db:
        assert (
            db.execute(select(func.count(User.id)).where(User.tenant_id == demo_tenant)).scalar_one()
            == 1
        )
        assert (
            db.execute(select(func.count(AIUsageLog.id)).where(AIUsageLog.tenant_id == demo_tenant)).scalar_one()
            == 0
        )
        assert (
            db.execute(select(func.count(AIUsageLog.id)).where(AIUsageLog.tenant_id == other_tenant)).scalar_one()
            == 1
        )
        assert (
            db.execute(
                select(func.count(User.id)).where(
                    User.tenant_id == demo_tenant,
                    getattr(User, "email", getattr(User, "username")) == "demo@demo.com",
                )
            ).scalar_one()
            == 1
        )
        assert db.get(User, other_user_id) is not None


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
        assert 'id="site-sidebar"' in tenants_page.text
        assert 'data-shell-toggle' in tenants_page.text
        assert 'data-shell-backdrop' in tenants_page.text
        assert ">Tenant Management<" in tenants_page.text
        assert ">AI Settings<" in tenants_page.text
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
        assert 'href="/admin/printing/agents"' in settings_page.text
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
        new_tenant_form = admin_client.get("/platform/tenants/new")
        assert new_tenant_form.status_code == 200
        assert "Initial tenant admin email" not in new_tenant_form.text
        assert "Initial tenant admin password" not in new_tenant_form.text
        assert "add the first tenant admin from tenant management" in new_tenant_form.text
        csrf = _prime_csrf(admin_client)

        missing_name = admin_client.post(
            "/platform/tenants/new",
            data={
                "name": "",
                "subdomain": "missing-name",
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
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert created.status_code in {302, 303}
        assert created.headers.get("location", "").startswith("/platform/tenants/")
        assert created.headers.get("location", "").endswith("?tenant_created=1")

        created_detail = admin_client.get(created.headers["location"])
        assert created_detail.status_code == 200
        assert "Tenant created. Add the first tenant user below before anyone can sign in to this workspace." in created_detail.text
        assert 'href="/t/mytenant/login"' in created_detail.text
        assert "https://mytenant.example.test/login" not in created_detail.text
        assert "No tenant users." in created_detail.text

    with SessionLocal() as db:
        tenant = db.execute(select(Tenant).where(Tenant.name == "Normalized Tenant")).scalars().first()
        assert tenant is not None
        assert tenant.subdomain == "mytenant"
        assert db.execute(select(User).where(User.tenant_id == int(tenant.id))).scalars().first() is None


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
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert created.status_code in {302, 303}
        assert created.headers.get("location", "").startswith("/platform/tenants/")
        assert created.headers.get("location", "").endswith("?tenant_created=1")

        created_detail = platform_client.get(created.headers["location"])
        assert created_detail.status_code == 200
        assert "Tenant created. Add the first tenant user below before anyone can sign in to this workspace." in created_detail.text
        assert 'href="https://customdomain.example.test/login"' in created_detail.text
        assert "/t/customdomain/login" not in created_detail.text

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
    assert 'data-dashboard-period="12m"' in response.text
    assert 'data-dashboard-period="30d"' not in response.text
    assert 'data-dashboard-period-active="1"' in response.text
    assert "Activity Overview" in response.text
    assert "Updated just now" in response.text
    assert "Overview Activity" not in response.text
    assert 'data-dashboard-panel="ai-insights"' not in response.text
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


def test_dashboard_legacy_30d_period_maps_to_12m():
    assert main_module._normalize_dashboard_period("30d") == "12m"


def test_dashboard_uses_uk_day_boundaries_and_renders_uk_clock(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-uk-time.db", monkeypatch=monkeypatch
    )
    fixed_now = datetime(2026, 4, 8, 23, 30, 0)
    monkeypatch.setattr(main_module, "utcnow", lambda: fixed_now)

    tenant_id = _seed_tenant(SessionLocal, name="UK Clock Co", subdomain="uk-clock")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="UK Clock Co",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="uk-clock-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with SessionLocal() as db:
        db.add(
            Ticket(
                tenant_id=tenant_id,
                ticket_no="UK-LOCAL-1",
                datetime=datetime(2026, 4, 9, 0, 10, 0),
                status=TicketStatusEnum.COMPLETE.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.SALE.value,
                net_kg=1000,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

    with _client(app, base_url="https://uk-clock.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="uk-clock-admin@example.com",
                password="TestPass123!",
                next_path="/?period=today",
            )
            == 303
        )
        response = tenant_client.get("/?period=today")

    assert response.status_code == 200
    assert 'data-dashboard-uk-clock="1"' in response.text
    assert 'data-dashboard-uk-clock-time' in response.text
    assert _dashboard_metric_value(response.text, "completed_today") == "1"
    assert 'data-dashboard-traffic-ticket="UK-LOCAL-1"' in response.text


def test_logged_in_tenant_home_dashboard_is_tenant_scoped_and_populated(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-data.db", monkeypatch=monkeypatch
    )
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(main_module, "utcnow", lambda: fixed_now)
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
        today = fixed_now
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
    assert 'data-dashboard-panel="ai-insights"' not in response.text
    assert 'data-dashboard-panel="weight-throughput"' in response.text
    assert 'data-dashboard-panel="invoice-activity"' in response.text
    assert 'data-dashboard-period="12m"' in response.text
    assert 'data-dashboard-period="30d"' not in response.text
    assert "dashboard-bar-chart" not in response.text
    assert _dashboard_metric_value(response.text, "open_tickets") == "1"
    assert _dashboard_metric_value(response.text, "completed_today") == "1"
    assert _dashboard_metric_value(response.text, "total_weight_today") == "1,500 kg"
    assert _dashboard_metric_value(response.text, "invoices_pending") == "2"
    assert "Total processed this period: 4 tickets" in response.text
    assert "Total processed this period: 5.5 tonnes" in response.text
    assert response.text.count("Total processed this period:") == 2
    assert 'data-dashboard-ticket="A-COMP-1"' in response.text
    assert 'data-dashboard-ticket="A-OPEN-1"' in response.text
    assert 'data-dashboard-open-ticket="A-OPEN-1"' in response.text
    assert 'data-dashboard-traffic-ticket="A-COMP-1"' in response.text
    assert response.text.count("dashboard-svg-chart--bar") == 2
    assert "dashboard-svg-chart--area" not in response.text
    assert "dashboard-chart-wrap" not in response.text
    assert 'data-dashboard-throughput-kg="1500"' in response.text
    assert 'data-dashboard-throughput-kg="1750"' in response.text
    assert 'data-dashboard-throughput-kg="2200"' in response.text
    assert "09:00" in response.text
    assert 'data-dashboard-invoice="INV-A-1"' in response.text
    assert 'data-dashboard-invoice-ready-ticket="A-COMP-1"' in response.text
    assert "<th>Vehicle</th>" in response.text
    assert response.text.count("<th>Vehicle</th>") == 2
    assert response.text.count("<th>Product</th>") == 1
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
    assert "dashboard-bar-chart" not in response_today.text
    assert response_today.text.count("dashboard-svg-chart--bar") == 2
    assert "dashboard-svg-chart--area" not in response_today.text
    assert "Total processed this period: 2 tickets" in response_today.text
    assert "Total processed this period: 1.5 tonnes" in response_today.text
    assert response_today.text.count("Total processed this period:") == 2
    assert 'data-dashboard-throughput-kg="1500"' in response_today.text
    assert 'data-dashboard-throughput-kg="1750"' not in response_today.text
    assert 'data-dashboard-throughput-kg="2200"' not in response_today.text
    assert 'data-dashboard-throughput-kg="3100"' not in response_today.text
    assert response_today.text.count(">00:00<") == 2
    assert response_today.text.count(">09:00<") >= 2
    assert response_today.text.count(">21:00<") == 2

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="a-admin@example.com",
                password="TestPass123!",
                next_path="/?period=12m",
            )
            == 303
        )
        response_12m = tenant_client.get("/?period=12m")

    assert response_12m.status_code == 200
    assert 'data-dashboard-period="12m"' in response_12m.text
    assert 'data-dashboard-period="30d"' not in response_12m.text
    assert 'data-dashboard-period-active="1"' in response_12m.text
    assert "dashboard-bar-chart" not in response_12m.text
    assert "Tickets Processed Per Month" in response_12m.text
    assert response_12m.text.count('data-dashboard-chart-day="') == 12
    assert response_12m.text.count('data-dashboard-throughput-point="') == 12
    assert 'data-dashboard-chart-day="Mar 26"' in response_12m.text
    assert 'data-dashboard-chart-day="Feb 26"' in response_12m.text
    assert response_12m.text.count("dashboard-svg-chart--area") == 2
    assert "dashboard-svg-chart--bar" not in response_12m.text
    assert "dashboard-trend" not in response_12m.text
    assert "dashboard-chart-wrap" not in response_12m.text
    assert "Total processed this period: 4 tickets" in response_12m.text
    assert "Total processed this period: 8.6 tonnes" in response_12m.text
    assert response_12m.text.count("Total processed this period:") == 2
    assert 'data-dashboard-throughput-kg="5450"' in response_12m.text
    assert 'data-dashboard-throughput-kg="3100"' in response_12m.text
    assert 'data-dashboard-throughput-kg="1500"' not in response_12m.text
    assert 'data-dashboard-ticket="A-20D-1"' in response_12m.text
    assert 'data-dashboard-invoice="INV-A-OLD"' in response_12m.text


def test_ticket_quick_create_uses_uk_local_datetime(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ticket-uk-time.db", monkeypatch=monkeypatch
    )
    fixed_now = datetime(2026, 4, 8, 23, 30, 0)
    monkeypatch.setattr("app.routes.tickets.utcnow", lambda: fixed_now)

    tenant_id = _seed_tenant(SessionLocal, name="Ticket Time Co", subdomain="ticket-time")
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Ticket Time Co",
        primary_color="#225577",
    )
    _seed_user(
        SessionLocal,
        email="ticket-time-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with _client(app, base_url="https://ticket-time.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="ticket-time-admin@example.com",
                password="TestPass123!",
                next_path="/tickets",
            )
            == 303
        )
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/tickets/new/quick",
            data={CSRF_FORM_FIELD: csrf},
            follow_redirects=False,
        )

    assert response.status_code in {302, 303}
    ticket_location = str(response.headers.get("location", ""))
    assert ticket_location.startswith("/tickets/")
    ticket_id = int(ticket_location.split("?", 1)[0].rstrip("/").split("/")[-1])

    with SessionLocal() as db:
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.datetime == datetime(2026, 4, 9, 0, 30, 0)


def test_dashboard_ai_insights_show_fallback_for_ai_enabled_tenant_without_activity(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-empty.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Dashboard AI Co",
        subdomain="dashai",
        ai_enabled=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Dashboard AI Co",
        primary_color="#224466",
    )
    _seed_user(
        SessionLocal,
        email="dashai-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with _client(app, base_url="https://dashai.localhost") as tenant_client:
        assert (
            _login(
                tenant_client,
                email="dashai-admin@example.com",
                password="TestPass123!",
                next_path="/",
            )
            == 303
        )
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' in response.text
    assert "AI Insights" in response.text
    assert "Not enough recent activity to generate insights yet." in response.text


def test_dashboard_ai_insights_can_be_disabled_globally(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-global-disable.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Dashboard AI Co",
        subdomain="dashai",
        ai_enabled=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Dashboard AI Co",
        primary_color="#1f5673",
    )
    _seed_user(
        SessionLocal,
        email="dash-admin@example.com",
        password="DashPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )
    _save_platform_ai_settings(
        SessionLocal,
        ai_dashboard_insights_enabled=False,
    )

    with _client(app, base_url="https://dashai.localhost") as tenant_client:
        assert _login(
            tenant_client,
            email="dash-admin@example.com",
            password="DashPass123!",
        ) == 303
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' not in response.text
    assert "AI Insights" not in response.text
    assert "Activity Overview" in response.text
    assert "Assistant" in response.text


def test_dashboard_ai_insights_can_be_enabled_for_one_tenant_when_platform_default_is_disabled(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-tenant-enable.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Dashboard AI Co",
        subdomain="dashai",
        ai_enabled=False,
        ai_dashboard_insights_override=True,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Dashboard AI Co",
        primary_color="#28556a",
    )
    _seed_user(
        SessionLocal,
        email="dash-admin@example.com",
        password="DashPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )
    _save_platform_ai_settings(
        SessionLocal,
        ai_dashboard_insights_enabled=False,
    )

    with _client(app, base_url="https://dashai.localhost") as tenant_client:
        assert _login(
            tenant_client,
            email="dash-admin@example.com",
            password="DashPass123!",
        ) == 303
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' in response.text
    assert "AI Insights" in response.text
    assert "Not enough recent activity to generate insights yet." in response.text
    assert 'data-assistant-open' not in response.text
    assert 'data-assistant-panel' not in response.text


def test_dashboard_ai_insights_can_be_disabled_for_one_tenant_when_platform_default_is_enabled(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-tenant-disable.db", monkeypatch=monkeypatch
    )
    tenant_id = _seed_tenant(
        SessionLocal,
        name="Dashboard AI Co",
        subdomain="dashai",
        ai_enabled=True,
        ai_dashboard_insights_override=False,
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Dashboard AI Co",
        primary_color="#174f73",
    )
    _seed_user(
        SessionLocal,
        email="dash-admin@example.com",
        password="DashPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with _client(app, base_url="https://dashai.localhost") as tenant_client:
        assert _login(
            tenant_client,
            email="dash-admin@example.com",
            password="DashPass123!",
        ) == 303
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' not in response.text
    assert "AI Insights" not in response.text
    assert "Activity Overview" in response.text
    assert "Assistant" in response.text


def test_dashboard_ai_insights_use_tenant_scoped_metrics_when_enabled(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights.db", monkeypatch=monkeypatch
    )
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(main_module, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b", ai_enabled=True)
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

    with SessionLocal() as db:
        customer_a = Customer(tenant_id=tenant_a, account_code="CUST-A", name="Tenant A Customer")
        customer_b = Customer(tenant_id=tenant_b, account_code="CUST-B", name="Tenant B Customer")
        vehicle_a = Vehicle(tenant_id=tenant_a, registration="A123 OPEN")
        vehicle_b = Vehicle(tenant_id=tenant_b, registration="B123 OPEN")
        db.add_all([customer_a, customer_b, vehicle_a, vehicle_b])
        db.flush()
        db.add_all(
            [
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-OPEN-1",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    net_kg=1250,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-COMP-1",
                    datetime=fixed_now - timedelta(hours=1),
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    net_kg=2400,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_b,
                    ticket_no="B-OPEN-1",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer_b.id,
                    vehicle_id=vehicle_b.id,
                    net_kg=9999,
                    dont_invoice=False,
                    paid=False,
                ),
                Invoice(
                    tenant_id=tenant_a,
                    invoice_no="INV-A-OD",
                    customer_id=customer_a.id,
                    invoice_date=fixed_now.date() - timedelta(days=14),
                    due_date=fixed_now.date() - timedelta(days=2),
                    status="OPEN",
                    net_total=Decimal("100.00"),
                    vat_total=Decimal("20.00"),
                    gross_total=Decimal("120.00"),
                ),
                Invoice(
                    tenant_id=tenant_b,
                    invoice_no="INV-B-OD",
                    customer_id=customer_b.id,
                    invoice_date=fixed_now.date() - timedelta(days=14),
                    due_date=fixed_now.date() - timedelta(days=3),
                    status="OPEN",
                    net_total=Decimal("200.00"),
                    vat_total=Decimal("40.00"),
                    gross_total=Decimal("240.00"),
                ),
            ]
        )
        db.commit()

    captured: dict[str, object] = {}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "- 1 open ticket is still awaiting completion.\n- 1 overdue invoice needs follow-up.\n- Tenant A Customer is the top customer today at 2.4 tonnes.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        response = tenant_client.get("/")

    assert response.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' in response.text
    assert "1 open ticket is still awaiting completion." in response.text
    assert "1 overdue invoice needs follow-up." in response.text
    assert "Tenant A Customer is the top customer today at 2.4 tonnes." in response.text
    payload = captured["payload"]
    assert payload["max_output_tokens"] == 220
    assert payload["reasoning"] == {"effort": "minimal"}
    assert payload["text"] == {"verbosity": "low"}
    assert "Do not list ticket numbers, invoice numbers, or long record lists." in payload["instructions"]
    assert ai_assistant_module.AI_PROMPT_INJECTION_GUARD in payload["instructions"]
    insight_input = payload["input"][0]["content"][0]["text"]
    assert "A-OPEN-1" not in insight_input
    assert "INV-A-OD" not in insight_input
    assert "Tenant A Customer" in insight_input
    assert "B-OPEN-1" not in insight_input
    assert "INV-B-OD" not in insight_input
    assert '"sample"' not in insight_input


def test_dashboard_ai_insights_reuse_recent_cached_result(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-cache.db", monkeypatch=monkeypatch
    )
    _ = app
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    ai_assistant_module._dashboard_insights_cache.clear()

    tenant_id = _seed_tenant(SessionLocal, name="Tenant Cache", subdomain="cache", ai_enabled=True)

    with SessionLocal() as db:
        customer = Customer(tenant_id=tenant_id, account_code="CUST-CACHE", name="Cache Customer")
        vehicle = Vehicle(tenant_id=tenant_id, registration="CACHE123")
        db.add_all([customer, vehicle])
        db.flush()
        db.add(
            Ticket(
                tenant_id=tenant_id,
                ticket_no="CACHE-OPEN-1",
                datetime=fixed_now,
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.WASTEIN.value,
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                net_kg=1500,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "- 1 open ticket is waiting for completion.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with SessionLocal() as db:
        first = ai_assistant_module.generate_dashboard_insights(db, tenant_id, model="gpt-5-mini")
        second = ai_assistant_module.generate_dashboard_insights(db, tenant_id, model="gpt-5-mini")

    assert call_count["value"] == 1
    assert first["items"] == ["1 open ticket is waiting for completion."]
    assert second["items"] == ["1 open ticket is waiting for completion."]


def test_dashboard_ai_insights_cache_is_tenant_scoped(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-cache-scope.db", monkeypatch=monkeypatch
    )
    _ = app
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    ai_assistant_module._dashboard_insights_cache.clear()

    tenant_a = _seed_tenant(SessionLocal, name="Tenant Cache A", subdomain="cachea", ai_enabled=True)
    tenant_b = _seed_tenant(SessionLocal, name="Tenant Cache B", subdomain="cacheb", ai_enabled=True)

    with SessionLocal() as db:
        for tenant_id, account_code, registration in (
            (tenant_a, "CUST-CACHE-A", "CACHEA123"),
            (tenant_b, "CUST-CACHE-B", "CACHEB123"),
        ):
            customer = Customer(tenant_id=tenant_id, account_code=account_code, name="Shared Metrics Customer")
            vehicle = Vehicle(tenant_id=tenant_id, registration=registration)
            db.add_all([customer, vehicle])
            db.flush()
            db.add(
                Ticket(
                    tenant_id=tenant_id,
                    ticket_no=f"CACHE-{tenant_id}",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer.id,
                    vehicle_id=vehicle.id,
                    net_kg=1500,
                    dont_invoice=False,
                    paid=False,
                )
            )
        db.commit()

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "- 1 open ticket is waiting for completion.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with SessionLocal() as db:
        tenant_a_result = ai_assistant_module.generate_dashboard_insights(db, tenant_a, model="gpt-5-mini")
        tenant_b_result = ai_assistant_module.generate_dashboard_insights(db, tenant_b, model="gpt-5-mini")

    assert call_count["value"] == 2
    assert tenant_a_result["items"] == ["1 open ticket is waiting for completion."]
    assert tenant_b_result["items"] == ["1 open ticket is waiting for completion."]


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
        assert "Try the demo" in marketing.text
        assert "Operations Dashboard" not in marketing.text

    with _client(app, base_url="https://admin.example.test") as admin_client:
        admin_root = admin_client.get("/", follow_redirects=False)
        assert admin_root.status_code in {302, 303}
        assert admin_root.headers.get("location") == "/platform/tenants"


def test_tenant_ai_assistant_query_uses_gpt_5_mini_with_tenant_scoped_context(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b", ai_enabled=True)
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
        customer_a = Customer(tenant_id=tenant_a, account_code="CUST-A", name="Tenant A Customer")
        customer_b = Customer(tenant_id=tenant_b, account_code="CUST-B", name="Tenant B Customer")
        vehicle_a = Vehicle(tenant_id=tenant_a, registration="A123 OPEN")
        vehicle_b = Vehicle(tenant_id=tenant_b, registration="B123 OPEN")
        db.add_all([customer_a, customer_b, vehicle_a, vehicle_b])
        db.flush()
        ticket_a = Ticket(
            tenant_id=tenant_a,
            ticket_no="A-OPEN-1",
            datetime=datetime(2026, 3, 12, 8, 0, 0),
            status=TicketStatusEnum.OPEN.value,
            direction=DirectionEnum.INWARD.value,
            transaction_type=TransactionTypeEnum.SALE.value,
            customer_id=customer_a.id,
            vehicle_id=vehicle_a.id,
            net_kg=1250,
            dont_invoice=False,
            paid=False,
        )
        ticket_b = Ticket(
            tenant_id=tenant_b,
            ticket_no="B-OPEN-1",
            datetime=datetime(2026, 3, 12, 9, 0, 0),
            status=TicketStatusEnum.OPEN.value,
            direction=DirectionEnum.INWARD.value,
            transaction_type=TransactionTypeEnum.SALE.value,
            customer_id=customer_b.id,
            vehicle_id=vehicle_b.id,
            net_kg=2400,
            dont_invoice=False,
            paid=False,
        )
        db.add_all([ticket_a, ticket_b])
        db.flush()
        ticket_a_id = int(ticket_a.id)
        customer_a_id = int(customer_a.id)
        vehicle_a_id = int(vehicle_a.id)
        db.commit()

    captured: dict[str, object] = {}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "There is 1 open ticket: A-OPEN-1.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "There is 1 open ticket.",
        "items": [
            {
                "record_type": "ticket",
                "record_id": ticket_a_id,
                "title": "A-OPEN-1",
                "href": f"/tickets/{ticket_a_id}",
                "meta": "12 Mar 2026 08:00 | Tenant A Customer | A123 OPEN",
                "links": [
                    {
                        "record_type": "customer",
                        "record_id": customer_a_id,
                        "label": "Tenant A Customer",
                        "href": f"/customers/{customer_a_id}",
                    },
                    {
                        "record_type": "vehicle",
                        "record_id": vehicle_a_id,
                        "label": "A123 OPEN",
                        "href": f"/vehicles/{vehicle_a_id}",
                    },
                ],
            }
        ],
    }
    assert "A-OPEN-1" not in response.json()["answer"]
    assert captured["api_key"] == "test-openai-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5-mini"
    assert payload["max_output_tokens"] == 320
    assert payload["reasoning"] == {"effort": "minimal"}
    assert payload["text"] == {"verbosity": "low"}
    assert payload["instructions"].startswith(ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT)
    assert ai_assistant_module.AI_PROMPT_INJECTION_GUARD in payload["instructions"]
    assert "Weighbridge Web operational assistant" in payload["instructions"]
    assert "Examples:" in payload["instructions"]
    assert ai_assistant_module.AI_PROMPT_INJECTION_GUARD in payload["instructions"]
    content_text = payload["input"][0]["content"][0]["text"]
    assert "A-OPEN-1" in content_text
    assert "B-OPEN-1" not in content_text
    assert "First line: direct answer." in content_text
    assert "Then up to 4 short bullet points only if useful." in content_text


def test_tenant_ai_assistant_ui_renders_for_enabled_tenant(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-ui.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        dashboard = tenant_client.get("/")

    assert dashboard.status_code == 200
    assert 'data-assistant-open' in dashboard.text
    assert 'data-assistant-panel' in dashboard.text
    assert 'data-endpoint="/api/assistant/query"' in dashboard.text
    assert "Ask about tickets, invoices, customers, and today's activity." in dashboard.text
    assert "Try asking..." in dashboard.text
    assert "Which tickets are still open?" in dashboard.text
    assert "How much weight did we process today?" in dashboard.text
    assert "Which invoices are unpaid?" in dashboard.text
    assert "Who is the top customer today?" in dashboard.text
    assert 'class="assistant-panel__response-placeholder"' in dashboard.text
    assert "Ask a question or use a quick prompt to get started." in dashboard.text
    assert "Open tickets" in dashboard.text
    assert "Open waste tickets" in dashboard.text
    assert "Ready to invoice" in dashboard.text
    assert "Today's tonnage" in dashboard.text
    assert "Unpaid invoices" in dashboard.text
    assert "Overdue invoices" in dashboard.text
    assert "Recent tickets" in dashboard.text
    assert "Top customer today" in dashboard.text
    assert 'data-assistant-followups' not in dashboard.text
    assert "Show open tickets" not in dashboard.text
    assert "Show unpaid invoices" not in dashboard.text
    assert "Show today's weight" not in dashboard.text
    assert "/static/js/assistant_panel.js" in dashboard.text


def test_tenant_ai_assistant_ui_hidden_when_disabled_for_tenant(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-ui-disabled.db", monkeypatch=monkeypatch
    )
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=False)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        dashboard = tenant_client.get("/")

    assert dashboard.status_code == 200
    assert 'data-assistant-open' not in dashboard.text
    assert 'data-assistant-panel' not in dashboard.text
    assert "/static/js/assistant_panel.js" not in dashboard.text


def test_tenant_ai_assistant_uses_tenant_selected_model_when_configured(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-model.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(
        SessionLocal,
        name="Tenant A",
        subdomain="a",
        ai_enabled=True,
        ai_model="gpt-5",
    )
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    captured: dict[str, object] = {}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "There are no open tickets.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"answer": "There are no open tickets.", "items": []}
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5"


def test_tenant_ai_assistant_returns_linkable_invoice_results(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-invoice-links.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with SessionLocal() as db:
        customer = Customer(tenant_id=tenant_a, account_code="CUST-A", name="Tenant A Customer")
        db.add(customer)
        db.flush()
        invoice = Invoice(
            tenant_id=tenant_a,
            invoice_no="INV-A-100",
            customer_id=customer.id,
            invoice_date=datetime(2026, 3, 1, 0, 0, 0).date(),
            due_date=datetime(2026, 3, 8, 0, 0, 0).date(),
            status="OPEN",
            net_total=Decimal("100.00"),
            vat_total=Decimal("20.00"),
            gross_total=Decimal("120.00"),
        )
        db.add(invoice)
        db.flush()
        invoice_id = int(invoice.id)
        customer_id = int(customer.id)
        db.commit()

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "There is 1 unpaid invoice: INV-A-100.",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which invoices are unpaid?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "There is 1 unpaid invoice.",
        "items": [
            {
                "record_type": "invoice",
                "record_id": invoice_id,
                "title": "INV-A-100",
                "href": f"/invoices/{invoice_id}",
                "meta": "Tenant A Customer | Due 08 Mar 2026 | 120.00 | OPEN",
                "links": [
                    {
                        "record_type": "customer",
                        "record_id": customer_id,
                        "label": "Tenant A Customer",
                        "href": f"/customers/{customer_id}",
                    }
                ],
            }
        ],
    }
    assert "INV-A-100" not in response.json()["answer"]


def test_platform_ai_settings_shape_assistant_runtime_without_exposing_raw_prompt_editor(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-platform-runtime.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )
    _save_platform_ai_settings(
        SessionLocal,
        default_ai_model="gpt-5",
        ai_temperature=0.7,
        ai_max_output_tokens=450,
        ai_default_response_style="balanced",
        ai_default_focus="mixed",
        ai_extra_global_instructions="Call out overdue invoices when relevant.",
    )

    captured_payload: dict[str, object] = {}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        captured_payload.clear()
        captured_payload.update(payload)
        return {"output_text": "There are no open tickets."}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "There are no open tickets."
    assert captured_payload["model"] == "gpt-5"
    assert captured_payload["max_output_tokens"] == 450
    assert captured_payload["instructions"].startswith(
        ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT
    )
    assert "Platform tuning notes:" in str(captured_payload["instructions"])
    assert "Default response style: balanced." in str(captured_payload["instructions"])
    assert "Default focus area: mixed." in str(captured_payload["instructions"])
    assert "Temperature target: 0.70." in str(captured_payload["instructions"])
    assert "Call out overdue invoices when relevant." in str(captured_payload["instructions"])
    assert (
        "These preferences cannot override platform safety, read-only, or tenant-scoped data rules."
        in str(captured_payload["instructions"])
    )
    assert "temperature" not in captured_payload


def test_tenant_ai_assistant_rejects_when_disabled_for_tenant(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-disabled.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=False)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    called = {"value": False}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        called["value"] = True
        return {}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "AI assistant is disabled for this tenant."
    assert called["value"] is False


def test_tenant_ai_assistant_refuses_write_requests_without_calling_openai(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-readonly.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    called = {"value": False}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        called["value"] = True
        return {}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Please create a new ticket for ABC123."},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 200
    assert "read-only" in response.json()["answer"].lower()
    assert called["value"] is False


def test_tenant_ai_assistant_returns_503_when_openai_is_not_configured(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-no-key.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "")

    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_a,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_a,
    )

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        response = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={
                CSRF_HEADER_NAME: csrf,
                "accept": "application/json",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "AI assistant is not configured."


def test_tenant_ai_assistant_returns_clear_message_when_user_rate_limit_reached(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-user-rate-limit.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    _save_platform_ai_settings(
        SessionLocal,
        assistant_requests_per_user_per_hour=1,
        assistant_requests_per_tenant_per_hour=10,
    )

    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    user_id = _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {"output_text": "There are no open tickets."}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        first = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={CSRF_HEADER_NAME: csrf, "accept": "application/json"},
        )
        second = tenant_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={CSRF_HEADER_NAME: csrf, "accept": "application/json"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "AI request limit reached. Please try again later."
    assert call_count["value"] == 1

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(AIUsageLog)
                .where(
                    AIUsageLog.tenant_id == tenant_id,
                    AIUsageLog.request_type == ai_usage_module.REQUEST_TYPE_ASSISTANT,
                )
                .order_by(AIUsageLog.id.asc())
            ).scalars()
        )

    assert len(rows) == 2
    assert rows[0].user_id == user_id
    assert rows[0].success is True
    assert rows[0].error_type is None
    assert rows[0].counted_toward_limit is True
    assert rows[1].user_id == user_id
    assert rows[1].success is False
    assert rows[1].error_type == ai_usage_module.ERROR_TYPE_RATE_LIMIT_USER
    assert rows[1].counted_toward_limit is False


def test_tenant_ai_assistant_returns_clear_message_when_tenant_rate_limit_reached(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-tenant-rate-limit.db", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    _save_platform_ai_settings(
        SessionLocal,
        assistant_requests_per_user_per_hour=10,
        assistant_requests_per_tenant_per_hour=1,
    )

    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )
    second_user_id = _seed_user(
        SessionLocal,
        email="ops@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {"output_text": "There are no open tickets."}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as first_client:
        assert _login(first_client, email="a-admin@example.com", password="TestPass123!") == 303
        first_csrf = _prime_csrf(first_client)
        first = first_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={CSRF_HEADER_NAME: first_csrf, "accept": "application/json"},
        )

    with _client(app, base_url="https://a.localhost") as second_client:
        assert _login(second_client, email="ops@example.com", password="TestPass123!") == 303
        second_csrf = _prime_csrf(second_client)
        second = second_client.post(
            "/api/assistant/query",
            json={"question": "Which tickets are still open?"},
            headers={CSRF_HEADER_NAME: second_csrf, "accept": "application/json"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == "AI request limit reached. Please try again later."
    assert call_count["value"] == 1

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(AIUsageLog)
                .where(
                    AIUsageLog.tenant_id == tenant_id,
                    AIUsageLog.request_type == ai_usage_module.REQUEST_TYPE_ASSISTANT,
                )
                .order_by(AIUsageLog.id.asc())
            ).scalars()
        )

    assert len(rows) == 2
    assert rows[0].success is True
    assert rows[0].counted_toward_limit is True
    assert rows[1].user_id == second_user_id
    assert rows[1].success is False
    assert rows[1].error_type == ai_usage_module.ERROR_TYPE_RATE_LIMIT_TENANT
    assert rows[1].counted_toward_limit is False


def test_ai_assistant_system_prompt_stays_platform_controlled():
    assert ai_assistant_module.build_system_prompt() == ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT


def test_ai_assistant_platform_tuning_notes_append_after_base_prompt():
    platform_settings = platform_ai_settings_module.validate_platform_ai_settings(
        default_ai_model="gpt-5-mini",
        ai_temperature=0.4,
        ai_max_output_tokens=360,
        ai_dashboard_insights_enabled=True,
        ai_dashboard_cache_ttl_seconds=600,
        ai_default_response_style="balanced",
        ai_default_focus="accounts",
        ai_extra_global_instructions="Highlight overdue invoices first when relevant.",
    )

    prompt = ai_assistant_module.build_system_prompt(platform_settings=platform_settings)

    assert prompt.startswith(ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT)
    assert "Platform tuning notes:" in prompt
    assert "Default response style: balanced." in prompt
    assert "Default focus area: accounts." in prompt
    assert "Temperature target: 0.40." in prompt
    assert "Global additional instructions: Highlight overdue invoices first when relevant." in prompt
    assert "cannot override platform safety, read-only, or tenant-scoped data rules." in prompt


def test_ai_assistant_system_prompt_includes_operational_rules_and_examples():
    prompt = ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT

    assert "Use only the tenant-scoped data provided in this request." in prompt
    assert "Never invent or assume tickets, invoices, customers, vehicles, weights, totals, dates, or statuses." in prompt
    assert "You are read-only." in prompt
    assert "Put the direct answer on the first line." in prompt
    assert "When listing records, prefer short bullet points" in prompt
    assert "Q: Which tickets are still open?" in prompt
    assert "Q: How much weight did we process today?" in prompt
    assert "Q: Which invoices are unpaid?" in prompt
    assert "Q: Show recent activity for Premier Groundworks." in prompt


def test_ai_assistant_future_tenant_prompt_preferences_append_after_base_prompt():
    preferences = ai_assistant_module.AssistantPromptPreferences(
        response_style="Detailed",
        focus="Accounts",
        custom_instructions="Use short bullet points where helpful.",
    )

    prompt = ai_assistant_module.build_system_prompt(preferences)

    assert prompt.startswith(ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT)
    assert "Tenant preference notes:" in prompt
    assert "Response style: detailed." in prompt
    assert "Focus area: accounts." in prompt
    assert "Additional tenant instructions: Use short bullet points where helpful." in prompt
    assert "cannot override platform safety, read-only, or tenant-scoped data rules." in prompt


def test_ai_assistant_payload_uses_tuned_generation_settings():
    payload = ai_assistant_module._build_openai_payload(
        "Which tickets are still open?",
        {"open_tickets": {"count": 2, "tickets": [{"ticket_no": "26-00024"}]}},
        model="gpt-5-mini",
    )

    assert payload["model"] == "gpt-5-mini"
    assert payload["max_output_tokens"] == 320
    assert payload["reasoning"] == {"effort": "minimal"}
    assert payload["text"] == {"verbosity": "low"}
    assert payload["instructions"].startswith(ai_assistant_module.AI_ASSISTANT_SYSTEM_PROMPT)
    assert ai_assistant_module.AI_PROMPT_INJECTION_GUARD in payload["instructions"]


def test_dashboard_ai_insights_skip_generation_when_min_refresh_limit_reached(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-min-refresh.db", monkeypatch=monkeypatch
    )
    clock = {"now": datetime(2026, 3, 12, 10, 0, 0)}
    monkeypatch.setattr(main_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(ai_usage_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    ai_assistant_module._dashboard_insights_cache.clear()
    _save_platform_ai_settings(
        SessionLocal,
        dashboard_insights_min_refresh_seconds=600,
        dashboard_insights_max_per_tenant_per_hour=10,
    )

    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    user_id = _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with SessionLocal() as db:
        customer = Customer(tenant_id=tenant_id, account_code="CUST-A", name="Tenant A Customer")
        vehicle = Vehicle(tenant_id=tenant_id, registration="A123 OPEN")
        db.add_all([customer, vehicle])
        db.flush()
        db.add(
            Ticket(
                tenant_id=tenant_id,
                ticket_no="A-OPEN-1",
                datetime=clock["now"],
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.WASTEIN.value,
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                net_kg=1250,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {"output_text": "- 1 open ticket is still awaiting completion."}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        first = tenant_client.get("/")
        ai_assistant_module._dashboard_insights_cache.clear()
        clock["now"] = clock["now"] + timedelta(seconds=120)
        second = tenant_client.get("/")

    assert first.status_code == 200
    assert second.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' in first.text
    assert 'data-dashboard-panel="ai-insights"' not in second.text
    assert "Activity Overview" in second.text
    assert call_count["value"] == 1

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(AIUsageLog)
                .where(
                    AIUsageLog.tenant_id == tenant_id,
                    AIUsageLog.request_type == ai_usage_module.REQUEST_TYPE_DASHBOARD_INSIGHTS,
                )
                .order_by(AIUsageLog.id.asc())
            ).scalars()
        )

    assert len(rows) == 2
    assert rows[0].user_id == user_id
    assert rows[0].success is True
    assert rows[0].counted_toward_limit is True
    assert rows[1].user_id == user_id
    assert rows[1].success is False
    assert rows[1].error_type == ai_usage_module.ERROR_TYPE_RATE_LIMIT_MIN_REFRESH
    assert rows[1].counted_toward_limit is False


def test_dashboard_ai_insights_skip_generation_when_hourly_limit_reached(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-dashboard-ai-insights-hourly-limit.db", monkeypatch=monkeypatch
    )
    clock = {"now": datetime(2026, 3, 12, 10, 0, 0)}
    monkeypatch.setattr(main_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(ai_usage_module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    ai_assistant_module._dashboard_insights_cache.clear()
    _save_platform_ai_settings(
        SessionLocal,
        dashboard_insights_min_refresh_seconds=60,
        dashboard_insights_max_per_tenant_per_hour=1,
    )

    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    _seed_tenant_baseline(
        SessionLocal,
        tenant_id=tenant_id,
        company_name="Tenant A Co",
        primary_color="#113355",
    )
    user_id = _seed_user(
        SessionLocal,
        email="a-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with SessionLocal() as db:
        customer = Customer(tenant_id=tenant_id, account_code="CUST-A", name="Tenant A Customer")
        vehicle = Vehicle(tenant_id=tenant_id, registration="A123 OPEN")
        db.add_all([customer, vehicle])
        db.flush()
        db.add(
            Ticket(
                tenant_id=tenant_id,
                ticket_no="A-OPEN-1",
                datetime=clock["now"],
                status=TicketStatusEnum.OPEN.value,
                direction=DirectionEnum.INWARD.value,
                transaction_type=TransactionTypeEnum.WASTEIN.value,
                customer_id=customer.id,
                vehicle_id=vehicle.id,
                net_kg=1250,
                dont_invoice=False,
                paid=False,
            )
        )
        db.commit()

    call_count = {"value": 0}

    def fake_openai_request(*, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        _ = api_key
        _ = payload
        call_count["value"] += 1
        return {"output_text": "- 1 open ticket is still awaiting completion."}

    monkeypatch.setattr(ai_assistant_module, "_post_responses_request", fake_openai_request)

    with _client(app, base_url="https://a.localhost") as tenant_client:
        assert _login(tenant_client, email="a-admin@example.com", password="TestPass123!") == 303
        first = tenant_client.get("/")
        ai_assistant_module._dashboard_insights_cache.clear()
        clock["now"] = clock["now"] + timedelta(seconds=120)
        second = tenant_client.get("/")

    assert first.status_code == 200
    assert second.status_code == 200
    assert 'data-dashboard-panel="ai-insights"' in first.text
    assert 'data-dashboard-panel="ai-insights"' not in second.text
    assert "Activity Overview" in second.text
    assert call_count["value"] == 1

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(AIUsageLog)
                .where(
                    AIUsageLog.tenant_id == tenant_id,
                    AIUsageLog.request_type == ai_usage_module.REQUEST_TYPE_DASHBOARD_INSIGHTS,
                )
                .order_by(AIUsageLog.id.asc())
            ).scalars()
        )

    assert len(rows) == 2
    assert rows[0].user_id == user_id
    assert rows[0].success is True
    assert rows[0].counted_toward_limit is True
    assert rows[1].user_id == user_id
    assert rows[1].success is False
    assert rows[1].error_type == ai_usage_module.ERROR_TYPE_RATE_LIMIT_HOURLY
    assert rows[1].counted_toward_limit is False


def test_ai_assistant_context_supports_open_waste_and_top_customer_queries(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-context.db", monkeypatch=monkeypatch
    )
    _ = app
    tenant_a = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    tenant_b = _seed_tenant(SessionLocal, name="Tenant B", subdomain="b", ai_enabled=True)
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: fixed_now)

    with SessionLocal() as db:
        customer_a = Customer(tenant_id=tenant_a, account_code="CUST-A", name="Tenant A Customer")
        customer_b = Customer(tenant_id=tenant_b, account_code="CUST-B", name="Tenant B Customer")
        vehicle_a = Vehicle(tenant_id=tenant_a, registration="A123 WASTE")
        vehicle_b = Vehicle(tenant_id=tenant_b, registration="B123 WASTE")
        db.add_all([customer_a, customer_b, vehicle_a, vehicle_b])
        db.flush()
        db.add_all(
            [
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-WASTE-OPEN",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    net_kg=1300,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_a,
                    ticket_no="A-COMP-TOP",
                    datetime=fixed_now - timedelta(hours=1),
                    status=TicketStatusEnum.COMPLETE.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer_a.id,
                    vehicle_id=vehicle_a.id,
                    net_kg=4200,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_b,
                    ticket_no="B-WASTE-OPEN",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer_b.id,
                    vehicle_id=vehicle_b.id,
                    net_kg=9999,
                    dont_invoice=False,
                    paid=False,
                ),
                Invoice(
                    tenant_id=tenant_a,
                    invoice_no="INV-A-OVERDUE",
                    customer_id=customer_a.id,
                    invoice_date=fixed_now.date() - timedelta(days=14),
                    due_date=fixed_now.date() - timedelta(days=2),
                    status="OPEN",
                    net_total=Decimal("100.00"),
                    vat_total=Decimal("20.00"),
                    gross_total=Decimal("120.00"),
                ),
                Invoice(
                    tenant_id=tenant_b,
                    invoice_no="INV-B-OVERDUE",
                    customer_id=customer_b.id,
                    invoice_date=fixed_now.date() - timedelta(days=14),
                    due_date=fixed_now.date() - timedelta(days=3),
                    status="OPEN",
                    net_total=Decimal("200.00"),
                    vat_total=Decimal("40.00"),
                    gross_total=Decimal("240.00"),
                ),
            ]
        )
        db.commit()

        context = ai_assistant_module.build_question_context(
            db,
            tenant_a,
            "Which waste tickets are still open, which invoices are overdue, and who is the top customer today?",
        )

    assert context["open_waste_tickets"]["count"] == 1
    assert context["open_waste_tickets"]["tickets"][0]["ticket_no"] == "A-WASTE-OPEN"
    assert context["overdue_invoices"]["count"] == 1
    assert context["overdue_invoices"]["invoices"][0]["invoice_no"] == "INV-A-OVERDUE"
    assert context["top_customer_today"]["customer"] == "Tenant A Customer"


def test_ai_assistant_topic_precedence_prefers_specific_queries():
    assert ai_assistant_data_module.detect_question_topics("Which tickets are still open?") == [
        "open_tickets"
    ]
    assert ai_assistant_data_module.detect_question_topics("Which waste tickets are still open?") == [
        "open_waste_tickets"
    ]
    assert ai_assistant_data_module.detect_question_topics("Which invoices are overdue?") == [
        "overdue_invoices"
    ]
    assert ai_assistant_data_module.detect_question_topics("Who is the top customer today?") == [
        "top_customer_today"
    ]
    assert ai_assistant_data_module.detect_question_topics(
        "Which waste tickets are still open and which invoices are overdue?"
    ) == ["open_waste_tickets", "overdue_invoices"]


def test_ai_assistant_specific_topic_selection_keeps_summary_and_cards_aligned(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path, db_name="tenant-ai-assistant-topic-alignment.db", monkeypatch=monkeypatch
    )
    _ = app
    fixed_now = datetime(2026, 3, 12, 10, 0, 0)
    monkeypatch.setattr(ai_assistant_module, "utcnow", lambda: fixed_now)

    tenant_id = _seed_tenant(SessionLocal, name="Tenant A", subdomain="a", ai_enabled=True)
    with SessionLocal() as db:
        customer = Customer(tenant_id=tenant_id, account_code="CUST-A", name="Tenant A Customer")
        vehicle = Vehicle(tenant_id=tenant_id, registration="A123 TEST")
        db.add_all([customer, vehicle])
        db.flush()
        db.add_all(
            [
                Ticket(
                    tenant_id=tenant_id,
                    ticket_no="A-WASTE-OPEN",
                    datetime=fixed_now,
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.WASTEIN.value,
                    customer_id=customer.id,
                    vehicle_id=vehicle.id,
                    net_kg=1300,
                    dont_invoice=False,
                    paid=False,
                ),
                Ticket(
                    tenant_id=tenant_id,
                    ticket_no="A-SALE-OPEN",
                    datetime=fixed_now - timedelta(minutes=30),
                    status=TicketStatusEnum.OPEN.value,
                    direction=DirectionEnum.INWARD.value,
                    transaction_type=TransactionTypeEnum.SALE.value,
                    customer_id=customer.id,
                    vehicle_id=vehicle.id,
                    net_kg=1700,
                    dont_invoice=False,
                    paid=False,
                ),
            ]
        )
        db.commit()

        question = "Which waste tickets are still open?"
        context = ai_assistant_module.build_question_context(db, tenant_id, question)
        items = ai_assistant_module._build_structured_result_items(question, context)
        summary = ai_assistant_module._build_structured_summary_answer(question, context)

    assert summary == "There is 1 open waste ticket."
    assert [item["title"] for item in items] == ["A-WASTE-OPEN"]


def test_new_tenant_creation_flow_seeds_usable_baseline_and_requires_user_creation_from_detail(
    tmp_path,
    monkeypatch,
):
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
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_tenant.status_code in {302, 303}
        assert create_tenant.headers.get("location", "").startswith("/platform/tenants/")

        created_detail = admin_client.get(create_tenant.headers["location"])
        assert created_detail.status_code == 200
        assert "Tenant created. Add the first tenant user below before anyone can sign in to this workspace." in created_detail.text
        assert "No tenant users." in created_detail.text

        csrf = _prime_csrf(admin_client)
        create_user = admin_client.post(
            create_tenant.headers["location"].split("?", 1)[0] + "/users",
            data={
                "user_email": "seeded-admin@example.com",
                "user_role": ROLE_TENANT_ADMIN,
                "user_password": "SeededPass123!",
                "confirm_password": "SeededPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_user.status_code in {302, 303}

    with _client(app, base_url="https://seeded.localhost") as tenant_client:
        assert _login(tenant_client, email="seeded-admin@example.com", password="SeededPass123!") == 303
        csrf = _prime_csrf(tenant_client)
        create_product = tenant_client.post(
            "/products/new",
            data={
                "code": "SEED-PROD-1",
                "description": "Seeded Product",
                "sale_type": "WEIGHT",
                "product_type": "sale",
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
