from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlsplit

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from decimal import Decimal

import app.routes.admin_accounting as admin_accounting_route
import app.services.accounting.quickbooks_client as quickbooks_client_module
import app.services.accounting.revenue_account_mapping as revenue_account_mapping_module
import app.services.accounting.tax_mapping as tax_mapping_module
from app.auth import ROLE_OPERATOR, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, hash_password, user_identity_kwargs
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import (
    AccountingConnection,
    AccountingRevenueAccountMap,
    AccountingSyncEvent,
    AccountingSyncJob,
    AccountingTaxMap,
    AuditEvent,
    Base,
    Customer,
    Invoice,
    InvoiceLine,
    Product,
    ProductGroup,
    TaxRate,
    Tenant,
    User,
)
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.services.accounting.revenue_account_mapping import ProviderRevenueAccount
from app.services.accounting.tax_mapping import ProviderTaxCodeOption, QuickBooksTaxDiscoveryState
from app.services.accounting.quickbooks_oauth import QuickBooksTokenBundle
from app.services.secrets import decrypt_string, encrypt_string
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
    monkeypatch.setattr(settings, "app_secret_key", "admin-accounting-test-secret")
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


def _seed_tenant(SessionLocal: sessionmaker, *, name: str, subdomain: str) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _seed_tenant_baseline(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    company_name: str,
) -> None:
    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_id
        db.info["platform_mode"] = False
        company = ensure_company_settings_row_exists(db)
        company.tenant_id = tenant_id
        company.name = company_name
        company.is_initialized = True
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        seed_required_reference_data(db)
        db.commit()


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int | None,
    email: str,
    password: str,
    role: str,
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


def _prime_csrf(client: TestClient, *, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    client.headers.update({CSRF_HEADER_NAME: token})
    return token


def _login(
    client: TestClient,
    *,
    email: str,
    password: str,
    next_path: str = "/",
) -> str:
    csrf = _prime_csrf(client)
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "next": next_path,
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    client.headers.update({CSRF_HEADER_NAME: csrf})
    return csrf


def _post_with_csrf(client: TestClient, path: str, data: dict[str, object] | None = None) -> TestClient:
    payload = dict(data or {})
    payload[CSRF_FORM_FIELD] = _prime_csrf(client)
    return client.post(
        path,
        data=payload,
        follow_redirects=False,
    )


def _fake_token_bundle(*, realm_id: str) -> QuickBooksTokenBundle:
    return QuickBooksTokenBundle(
        access_token="qb-access-token",
        refresh_token="qb-refresh-token",
        access_token_expires_at=datetime(2027, 4, 17, 12, 0, 0),
        refresh_token_expires_at=datetime(2027, 9, 1, 12, 0, 0),
        scopes="com.intuit.quickbooks.accounting",
        realm_id=realm_id,
        raw_response={"token_type": "bearer"},
    )


def _seed_connected_connection(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    realm_id: str,
) -> int:
    with SessionLocal() as db:
        connection = AccountingConnection(
            tenant_id=tenant_id,
            provider="quickbooks",
            status="connected",
            realm_id=realm_id,
            encrypted_access_token=encrypt_string("qb-access-token"),
            encrypted_refresh_token=encrypt_string("qb-refresh-token"),
            access_token_expires_at=datetime(2027, 4, 17, 12, 0, 0),
            refresh_token_expires_at=datetime(2027, 9, 1, 12, 0, 0),
            scopes="com.intuit.quickbooks.accounting",
            connected_at=datetime(2027, 4, 16, 12, 0, 0),
            disconnected_at=None,
            last_error=None,
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        return int(connection.id)


def _authorize_state_from_redirect(location: str) -> str:
    query = parse_qs(urlsplit(location).query)
    state_values = query.get("state", [])
    assert state_values
    return str(state_values[0])


def _oauth_redirect_uri_from_redirect(location: str) -> str:
    query = parse_qs(urlsplit(location).query)
    redirect_values = query.get("redirect_uri", [])
    assert redirect_values
    return str(redirect_values[0])


def _connection_for_tenant(SessionLocal: sessionmaker, tenant_id: int) -> AccountingConnection | None:
    with SessionLocal() as db:
        return (
            db.execute(
                select(AccountingConnection).where(
                    AccountingConnection.tenant_id == tenant_id,
                    AccountingConnection.provider == "quickbooks",
                )
            )
            .scalars()
            .first()
        )


def _tax_maps_for_tenant(SessionLocal: sessionmaker, tenant_id: int) -> list[AccountingTaxMap]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(AccountingTaxMap)
                .where(
                    AccountingTaxMap.tenant_id == tenant_id,
                    AccountingTaxMap.provider == "quickbooks",
                )
                .order_by(AccountingTaxMap.id.asc())
            ).scalars()
        )


def _revenue_account_maps_for_tenant(
    SessionLocal: sessionmaker,
    tenant_id: int,
) -> list[AccountingRevenueAccountMap]:
    with SessionLocal() as db:
        return list(
            db.execute(
                select(AccountingRevenueAccountMap)
                .where(
                    AccountingRevenueAccountMap.tenant_id == tenant_id,
                    AccountingRevenueAccountMap.provider == "quickbooks",
                )
                .order_by(AccountingRevenueAccountMap.id.asc())
            ).scalars()
        )


def _audit_actions(SessionLocal: sessionmaker, tenant_id: int) -> set[str]:
    with SessionLocal() as db:
        return {
            str(row.action)
            for row in db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
            ).scalars()
        }


def _sync_event_types(SessionLocal: sessionmaker, tenant_id: int) -> set[str]:
    with SessionLocal() as db:
        return {
            str(row.event_type)
            for row in db.execute(
                select(AccountingSyncEvent).where(
                    AccountingSyncEvent.tenant_id == tenant_id,
                    AccountingSyncEvent.provider == "quickbooks",
                )
            ).scalars()
        }


def _client_for(app: FastAPI, *, base_url: str) -> TestClient:
    return TestClient(app, base_url=base_url)


def _tax_discovery_state(
    options: list[ProviderTaxCodeOption] | None = None,
    *,
    using_sales_tax: bool | None = True,
    partner_tax_enabled: bool | None = False,
    company_country: str | None = "GB",
    company_country_subdivision_code: str | None = None,
    tax_rate_count: int = 0,
    tax_agency_count: int = 0,
    tax_code_error: str | None = None,
    tax_rate_error: str | None = None,
    tax_agency_error: str | None = None,
    preferences_error: str | None = None,
    company_info_error: str | None = None,
) -> QuickBooksTaxDiscoveryState:
    return QuickBooksTaxDiscoveryState(
        provider_tax_code_options=list(options or []),
        using_sales_tax=using_sales_tax,
        partner_tax_enabled=partner_tax_enabled,
        company_country=company_country,
        company_country_subdivision_code=company_country_subdivision_code,
        tax_rate_count=tax_rate_count,
        tax_agency_count=tax_agency_count,
        tax_code_error=tax_code_error,
        tax_rate_error=tax_rate_error,
        tax_agency_error=tax_agency_error,
        preferences_error=preferences_error,
        company_info_error=company_info_error,
    )


def _prepare_environment(tmp_path: Path, monkeypatch):
    return _prepare_environment_with_settings(
        tmp_path,
        monkeypatch,
        quickbooks_redirect_uri="https://admin.localhost/api/accounting/quickbooks/callback",
        base_domain="",
    )


def _prepare_environment_with_settings(
    tmp_path: Path,
    monkeypatch,
    *,
    quickbooks_redirect_uri: str,
    base_domain: str,
):
    monkeypatch.setattr(settings, "quickbooks_client_id", "qb-client-id")
    monkeypatch.setattr(settings, "quickbooks_client_secret", "qb-client-secret")
    monkeypatch.setattr(settings, "quickbooks_redirect_uri", quickbooks_redirect_uri)
    monkeypatch.setattr(settings, "quickbooks_environment", "sandbox")
    monkeypatch.setattr(settings, "base_domain", base_domain)
    monkeypatch.setattr(
        settings,
        "app_encryption_key",
        Fernet.generate_key().decode("ascii"),
    )
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="admin-accounting.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    other_tenant_id = _seed_tenant(SessionLocal, name="Other", subdomain="other")
    _seed_tenant_baseline(SessionLocal, tenant_id=tenant_id, company_name="Acme")
    _seed_tenant_baseline(SessionLocal, tenant_id=other_tenant_id, company_name="Other")
    password = "TestPass123!"
    admin_email = "admin@acme.example"
    other_admin_email = "admin@other.example"
    operator_email = "operator@acme.example"
    _seed_user(
        SessionLocal,
        tenant_id=tenant_id,
        email=admin_email,
        password=password,
        role=ROLE_TENANT_ADMIN,
    )
    _seed_user(
        SessionLocal,
        tenant_id=tenant_id,
        email=operator_email,
        password=password,
        role=ROLE_OPERATOR,
    )
    _seed_user(
        SessionLocal,
        tenant_id=other_tenant_id,
        email=other_admin_email,
        password=password,
        role=ROLE_TENANT_ADMIN,
    )
    _seed_user(
        SessionLocal,
        tenant_id=None,
        email="superadmin@example.com",
        password=password,
        role=ROLE_SUPERADMIN,
    )
    monkeypatch.setattr(
        admin_accounting_route,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        revenue_account_mapping_module,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(),
    )
    monkeypatch.setattr(
        tax_mapping_module,
        "list_provider_tax_codes",
        lambda *args, **kwargs: [],
    )
    return {
        "app": app,
        "SessionLocal": SessionLocal,
        "tenant_id": tenant_id,
        "other_tenant_id": other_tenant_id,
        "password": password,
        "admin_email": admin_email,
        "other_admin_email": other_admin_email,
        "operator_email": operator_email,
    }


def test_tenant_admin_can_access_admin_accounting(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = client.get("/admin/accounting")
        assert response.status_code == 200
        assert "Accounting Integrations" in response.text
        assert "QuickBooks Connection" in response.text
        assert "Manage Tax Mappings" in response.text
        assert "Setup Status" in response.text
        assert "Latest Sync Jobs" in response.text
        assert "Latest Sync Events" in response.text
        assert "qb-client-secret" not in response.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_admin_accounting_can_view_and_save_default_revenue_account_mapping(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-revenue-account",
    )
    monkeypatch.setattr(
        admin_accounting_route,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )
    revenue_accounts = [
        ProviderRevenueAccount(
            remote_account_id="79",
            remote_account_code="4100",
            remote_account_name="Sales Income",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="4100 - Sales Income (Income)",
        ),
        ProviderRevenueAccount(
            remote_account_id="88",
            remote_account_code="4200",
            remote_account_name="Skip Hire Sales",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="4200 - Skip Hire Sales (Income)",
        ),
    ]
    monkeypatch.setattr(
        revenue_account_mapping_module,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")

        page = client.get("/admin/accounting")
        assert page.status_code == 200
        assert "Default Revenue Account" in page.text
        assert "Where invoice revenue is posted in QuickBooks. Usually 'Sales'." in " ".join(
            page.text.split()
        )
        assert "4100 - Sales Income (Income)" in page.text
        assert "4200 - Skip Hire Sales (Income)" in page.text
        assert "No default selected - use product nominal-code fallback" in page.text

        save_response = _post_with_csrf(
            client,
            "/admin/accounting/revenue-account",
            data={"remote_account_id": "79"},
        )
        assert save_response.status_code == 303
        assert save_response.headers["location"] == "/admin/accounting?revenue_account_saved=1"

        mappings = _revenue_account_maps_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert len(mappings) == 1
        assert mappings[0].local_scope_type == "global_default"
        assert mappings[0].local_scope_id is None
        assert mappings[0].local_nominal_code is None
        assert mappings[0].remote_account_id == "79"
        assert mappings[0].remote_account_code == "4100"
        assert mappings[0].remote_account_name == "Sales Income"
        assert mappings[0].remote_account_type == "Income"
        assert mappings[0].is_active is True

        follow = client.get(save_response.headers["location"])
        assert follow.status_code == 200
        assert "Default revenue account saved." in follow.text
        assert "Saved default:" in follow.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_default_revenue_account_mapping_preserves_tenant_isolation(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-acme-revenue",
    )
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["other_tenant_id"],
        realm_id="realm-other-revenue",
    )
    monkeypatch.setattr(
        admin_accounting_route,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )
    revenue_accounts = [
        ProviderRevenueAccount(
            remote_account_id="79",
            remote_account_code="4100",
            remote_account_name="Sales Income",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="4100 - Sales Income (Income)",
        ),
        ProviderRevenueAccount(
            remote_account_id="88",
            remote_account_code="4200",
            remote_account_name="Skip Hire Sales",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="4200 - Skip Hire Sales (Income)",
        ),
    ]
    monkeypatch.setattr(
        revenue_account_mapping_module,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )
    with env["SessionLocal"]() as db:
        db.add(
            AccountingRevenueAccountMap(
                tenant_id=env["tenant_id"],
                provider="quickbooks",
                local_scope_type="global_default",
                remote_account_id="79",
                remote_account_code="4100",
                remote_account_name="Sales Income",
                remote_account_type="Income",
                is_active=True,
            )
        )
        db.commit()

    other_client = _client_for(env["app"], base_url="https://other.localhost")
    try:
        _login(
            other_client,
            email=env["other_admin_email"],
            password=env["password"],
            next_path="/admin",
        )
        response = _post_with_csrf(
            other_client,
            "/admin/accounting/revenue-account",
            data={"remote_account_id": "88"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/accounting?revenue_account_saved=1"

        acme_mappings = _revenue_account_maps_for_tenant(env["SessionLocal"], env["tenant_id"])
        other_mappings = _revenue_account_maps_for_tenant(
            env["SessionLocal"], env["other_tenant_id"]
        )
        assert len(acme_mappings) == 1
        assert acme_mappings[0].remote_account_id == "79"
        assert len(other_mappings) == 1
        assert other_mappings[0].remote_account_id == "88"
    finally:
        other_client.close()
        env["app"].dependency_overrides.clear()


def test_admin_accounting_revenue_account_dropdown_prefers_sales_income_and_hides_non_income(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-revenue-suggested",
    )
    revenue_accounts = [
        ProviderRevenueAccount(
            remote_account_id="77",
            remote_account_code="4100",
            remote_account_name="Consulting Income",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="ignored",
        ),
        ProviderRevenueAccount(
            remote_account_id="79",
            remote_account_code="4000",
            remote_account_name="Sales",
            remote_account_type="Income",
            remote_account_detail_type="SalesOfProductIncome",
            is_active=True,
            is_usable=True,
            display_label="ignored",
        ),
        ProviderRevenueAccount(
            remote_account_id="91",
            remote_account_code="5000",
            remote_account_name="Cost Of Sales",
            remote_account_type="Expense",
            remote_account_detail_type="CostOfLabor",
            is_active=True,
            is_usable=False,
            display_label="ignored",
        ),
    ]
    monkeypatch.setattr(
        admin_accounting_route,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )
    monkeypatch.setattr(
        revenue_account_mapping_module,
        "list_provider_revenue_accounts",
        lambda *args, **kwargs: revenue_accounts,
    )

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        page = client.get("/admin/accounting")
        assert page.status_code == 200
        assert "Default Revenue Account" in page.text
        assert "Where invoice revenue is posted in QuickBooks. Usually 'Sales'." in " ".join(
            page.text.split()
        )
        assert "4000 - Sales (Income)" in page.text
        assert "4100 - Consulting Income (Income)" in page.text
        assert "5000 - Cost Of Sales" not in page.text
        assert '<option value="79" selected>' in page.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_admin_accounting_shows_tax_mapping_blockers(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-blockers",
    )
    with env["SessionLocal"]() as db:
        tax_rate_mapped = TaxRate(
            code="VAT20-ADMIN",
            description="VAT 20",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        tax_rate_missing = TaxRate(
            code="ZERO-ADMIN",
            description="Zero",
            rate_percent=Decimal("0.000"),
            is_active=True,
        )
        db.add_all([tax_rate_mapped, tax_rate_missing])
        db.flush()
        db.add_all(
            [
                Product(
                    tenant_id=env["tenant_id"],
                    code="ADMIN-PROD-1",
                    description="Admin Product One",
                    nominal_code="4000",
                    unit_price=Decimal("10.00"),
                    tax_rate_id=tax_rate_mapped.id,
                ),
                Product(
                    tenant_id=env["tenant_id"],
                    code="ADMIN-PROD-2",
                    description="Admin Product Two",
                    unit_price=Decimal("12.00"),
                    tax_rate_id=tax_rate_missing.id,
                ),
                Product(
                    tenant_id=env["tenant_id"],
                    code="ADMIN-PROD-3",
                    description="Admin Product Three",
                    unit_price=Decimal("8.00"),
                ),
                AccountingTaxMap(
                    tenant_id=env["tenant_id"],
                    provider="quickbooks",
                    tax_rate_id=tax_rate_mapped.id,
                    external_id="3",
                    external_code="20.0% S",
                    is_active=True,
                ),
                AccountingSyncJob(
                    tenant_id=env["tenant_id"],
                    provider="quickbooks",
                    job_type="sync_invoice",
                    entity_type="invoice",
                    entity_id=101,
                    status="pending",
                    attempts=0,
                    available_at=datetime(2026, 4, 16, 10, 0, 0),
                ),
                AccountingSyncJob(
                    tenant_id=env["tenant_id"],
                    provider="quickbooks",
                    job_type="sync_invoice",
                    entity_type="invoice",
                    entity_id=102,
                    status="failed",
                    attempts=1,
                    available_at=datetime(2026, 4, 16, 10, 0, 0),
                    error_text="Missing tax map",
                ),
            ]
        )
        db.commit()

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = client.get("/admin/accounting")
        assert response.status_code == 200
        assert "Setup Status" in response.text
        assert "Required Tax Mappings" in response.text
        assert "Missing Tax Mappings" in response.text
        assert "Products Missing Tax Rate" in response.text
        assert "Products Missing Nominal Fallback" in response.text
        assert "Pending Sync Jobs" in response.text
        assert "Failed Sync Jobs" in response.text
        assert "Next Steps" in response.text
        assert "Connect this tenant to the QuickBooks sandbox company" not in response.text
        assert "VAT20-ADMIN" in response.text
        assert "ZERO-ADMIN" in response.text
        assert "20.0% S" in response.text
        assert "Missing" in response.text
        assert "Review Tax Mappings" in response.text
        assert "qb-access-token" not in response.text
        assert "qb-refresh-token" not in response.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_admin_accounting_shows_invoice_account_mismatch_context_for_failed_jobs(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-account-mismatch",
    )
    with env["SessionLocal"]() as db:
        customer = Customer(
            tenant_id=env["tenant_id"],
            account_code="C-ADMIN-ACCT-FAIL",
            name="Accounting Failure Customer",
        )
        product_group = ProductGroup(
            tenant_id=env["tenant_id"],
            code="ADMIN-ACCT-GROUP",
            name="Admin Account Group",
            nominal_code_default="4000",
            is_active=True,
        )
        product = Product(
            tenant_id=env["tenant_id"],
            code="ADMIN-ACCT-PROD",
            description="Admin Account Product",
            product_group=product_group,
            unit_price=Decimal("10.00"),
        )
        db.add_all([customer, product_group, product])
        db.flush()
        invoice = Invoice(
            tenant_id=env["tenant_id"],
            invoice_no="INV-ADMIN-ACCT-FAIL",
            customer_id=customer.id,
            invoice_date=date(2026, 4, 16),
            due_date=date(2026, 5, 16),
            status="DRAFT",
            net_total=Decimal("10.00"),
            vat_total=Decimal("2.00"),
            gross_total=Decimal("12.00"),
        )
        db.add(invoice)
        db.flush()
        db.add(
            InvoiceLine(
                tenant_id=env["tenant_id"],
                invoice_id=invoice.id,
                description="Admin Account Failure Line",
                quantity=Decimal("1.000"),
                unit_price=Decimal("10.00"),
                net=Decimal("10.00"),
                vat=Decimal("2.00"),
                gross=Decimal("12.00"),
                product_snapshot_json={
                    "product_id": product.id,
                    "product_code": product.code,
                    "nominal_code": "4000",
                },
            )
        )
        db.add(
            AccountingSyncJob(
                tenant_id=env["tenant_id"],
                provider="quickbooks",
                job_type="sync_invoice",
                entity_type="invoice",
                entity_id=invoice.id,
                status="failed",
                attempts=2,
                available_at=datetime(2026, 4, 16, 10, 0, 0),
                error_text="QuickBooks income account with AcctNum 4000 was not found.",
            )
        )
        db.commit()
        invoice_id = int(invoice.id)
        product_id = int(product.id)

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = client.get("/admin/accounting")
        assert response.status_code == 200
        page_text = " ".join(response.text.split())
        assert (
            "Retry Failed Jobs retries the existing failed jobs below. "
            "It does not create a fresh invoice. "
            "After fixing product/account setup, you may need to create and sync a new invoice instead."
        ) in page_text
        assert (
            f"Invoice {invoice_id} requested AcctNum 4000. "
            "Retry Failed Jobs retries this existing failed job; it does not create a fresh invoice."
        ) in page_text
        assert (
            f"Product {product_id} ADMIN-ACCT-PROD - Admin Account Product "
            "(product group default nominal code: 4000)"
        ) in page_text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_non_admin_cannot_access_admin_accounting(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    operator_client = _client_for(env["app"], base_url="https://acme.localhost")
    platform_client = _client_for(env["app"], base_url="https://admin.localhost")
    try:
        _login(
            operator_client,
            email=env["operator_email"],
            password=env["password"],
            next_path="/",
        )
        operator_response = operator_client.get("/admin/accounting")
        assert operator_response.status_code == 403

        _login(
            platform_client,
            email="superadmin@example.com",
            password=env["password"],
            next_path="/platform/tenants",
        )
        platform_response = platform_client.get("/admin/accounting")
        assert platform_response.status_code == 404
    finally:
        operator_client.close()
        platform_client.close()
        env["app"].dependency_overrides.clear()


def test_connect_route_redirects_to_quickbooks_authorize_url(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = client.get("/admin/accounting/quickbooks/connect", follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://appcenter.intuit.com/connect/oauth2?")
        query = parse_qs(urlsplit(location).query)
        assert query["client_id"] == ["qb-client-id"]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["com.intuit.quickbooks.accounting"]
        assert _oauth_redirect_uri_from_redirect(location) == (
            "https://admin.localhost/api/accounting/quickbooks/callback"
        )
        assert _authorize_state_from_redirect(location)
        assert "ACCOUNTING_CONNECT_START" in _audit_actions(env["SessionLocal"], env["tenant_id"])
        assert "oauth_connect_started" in _sync_event_types(env["SessionLocal"], env["tenant_id"])
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_connect_route_uses_canonical_platform_callback_on_custom_domain(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment_with_settings(
        tmp_path,
        monkeypatch,
        quickbooks_redirect_uri="https://admin.roberthetherington.com/api/accounting/quickbooks/callback",
        base_domain="roberthetherington.com",
    )
    client = _client_for(env["app"], base_url="https://acme.roberthetherington.com")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = client.get("/admin/accounting/quickbooks/connect", follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["location"]
        assert _oauth_redirect_uri_from_redirect(location) == (
            "https://admin.roberthetherington.com/api/accounting/quickbooks/callback"
        )
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_callback_with_invalid_state_fails_safely(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    client = _client_for(env["app"], base_url="https://admin.localhost")
    try:
        response = client.get(
            "/api/accounting/quickbooks/callback?code=auth-code&realmId=realm-123",
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert response.text == "QuickBooks callback could not be verified."
        assert _connection_for_tenant(env["SessionLocal"], env["tenant_id"]) is None
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_canonical_callback_route_is_not_available_on_tenant_host(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        response = client.get(
            "/api/accounting/quickbooks/callback?state=invalid&code=auth-code",
            follow_redirects=False,
        )
        assert response.status_code == 404
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_callback_success_stores_encrypted_tokens_and_realm_id(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    tenant_client = _client_for(env["app"], base_url="https://acme.localhost")
    platform_client = _client_for(env["app"], base_url="https://admin.localhost")
    monkeypatch.setattr(
        admin_accounting_route,
        "exchange_code_for_tokens",
        lambda **kwargs: _fake_token_bundle(realm_id=str(kwargs["realm_id"])),
    )
    try:
        _login(tenant_client, email=env["admin_email"], password=env["password"], next_path="/admin")
        connect_response = tenant_client.get(
            "/admin/accounting/quickbooks/connect",
            follow_redirects=False,
        )
        state = _authorize_state_from_redirect(connect_response.headers["location"])

        callback = platform_client.get(
            f"/api/accounting/quickbooks/callback?state={state}&code=auth-code&realmId=realm-123",
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == (
            "https://acme.localhost/admin/accounting?quickbooks_connected=1"
        )

        connection = _connection_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert connection is not None
        assert connection.status == "connected"
        assert connection.provider == "quickbooks"
        assert connection.realm_id == "realm-123"
        assert connection.encrypted_access_token
        assert connection.encrypted_access_token != "qb-access-token"
        assert connection.encrypted_refresh_token
        assert connection.encrypted_refresh_token != "qb-refresh-token"
        assert decrypt_string(connection.encrypted_access_token) == "qb-access-token"
        assert decrypt_string(connection.encrypted_refresh_token) == "qb-refresh-token"
        assert connection.scopes == "com.intuit.quickbooks.accounting"
        assert connection.last_error is None

        accounting_page = tenant_client.get("/admin/accounting")
        assert accounting_page.status_code == 200
        assert "qb-access-token" not in accounting_page.text
        assert "qb-refresh-token" not in accounting_page.text
        assert "qb-client-secret" not in accounting_page.text
        assert "realm-123" in accounting_page.text
        assert "ACCOUNTING_CONNECTED" in _audit_actions(
            env["SessionLocal"], env["tenant_id"]
        )
        assert "oauth_connected" in _sync_event_types(
            env["SessionLocal"], env["tenant_id"]
        )
    finally:
        tenant_client.close()
        platform_client.close()
        env["app"].dependency_overrides.clear()


def test_callback_requires_initiating_user_to_still_match_tenant_admin_binding(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    tenant_client = _client_for(env["app"], base_url="https://acme.localhost")
    platform_client = _client_for(env["app"], base_url="https://admin.localhost")
    try:
        _login(tenant_client, email=env["admin_email"], password=env["password"], next_path="/admin")
        connect_response = tenant_client.get(
            "/admin/accounting/quickbooks/connect",
            follow_redirects=False,
        )
        state = _authorize_state_from_redirect(connect_response.headers["location"])

        with env["SessionLocal"]() as db:
            user = (
                db.execute(
                    select(User).where(User.email == env["admin_email"])
                )
                .scalars()
                .first()
            )
            assert user is not None
            user.tenant_id = env["other_tenant_id"]
            db.commit()

        callback = platform_client.get(
            f"/api/accounting/quickbooks/callback?state={state}&code=auth-code&realmId=realm-123",
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == (
            "https://acme.localhost/admin/accounting?error=QuickBooks+callback+could+not+be+verified."
        )
        assert _connection_for_tenant(env["SessionLocal"], env["tenant_id"]) is None
    finally:
        tenant_client.close()
        platform_client.close()
        env["app"].dependency_overrides.clear()


def test_disconnect_clears_tokens_and_updates_status(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-123",
    )
    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = _post_with_csrf(client, "/admin/accounting/quickbooks/disconnect")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/accounting?quickbooks_disconnected=1"

        connection = _connection_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert connection is not None
        assert connection.status == "disconnected"
        assert connection.encrypted_access_token is None
        assert connection.encrypted_refresh_token is None
        assert connection.access_token_expires_at is None
        assert connection.refresh_token_expires_at is None
        assert connection.realm_id == "realm-123"
        assert connection.disconnected_at is not None
        assert "ACCOUNTING_DISCONNECTED" in _audit_actions(
            env["SessionLocal"], env["tenant_id"]
        )
        assert "oauth_disconnected" in _sync_event_types(
            env["SessionLocal"], env["tenant_id"]
        )
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_tenant_isolation_is_preserved_for_quickbooks_connection(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-123",
    )
    other_client = _client_for(env["app"], base_url="https://other.localhost")
    try:
        _login(
            other_client,
            email=env["other_admin_email"],
            password=env["password"],
            next_path="/admin",
        )
        page = other_client.get("/admin/accounting")
        assert page.status_code == 200
        assert "realm-123" not in page.text
        assert "Disconnected" in page.text

        disconnect = _post_with_csrf(other_client, "/admin/accounting/quickbooks/disconnect")
        assert disconnect.status_code == 303
        assert "QuickBooks connection was not found." in unquote_plus(
            disconnect.headers["location"]
        )

        acme_connection = _connection_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert acme_connection is not None
        assert acme_connection.status == "connected"
        assert acme_connection.realm_id == "realm-123"
        assert _connection_for_tenant(env["SessionLocal"], env["other_tenant_id"]) is None
    finally:
        other_client.close()
        env["app"].dependency_overrides.clear()


def test_manual_run_sync_only_processes_current_tenant_jobs(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-acme",
    )
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["other_tenant_id"],
        realm_id="realm-other",
    )
    with env["SessionLocal"]() as db:
        customer_one = Customer(
            tenant_id=env["tenant_id"],
            account_code="C-ADMIN-RUN-1",
            name="Admin Run One",
        )
        customer_two = Customer(
            tenant_id=env["other_tenant_id"],
            account_code="C-ADMIN-RUN-2",
            name="Admin Run Two",
        )
        db.add_all([customer_one, customer_two])
        db.flush()
        db.add_all(
            [
                AccountingSyncJob(
                    tenant_id=env["tenant_id"],
                    provider="quickbooks",
                    job_type="sync_customer",
                    entity_type="customer",
                    entity_id=customer_one.id,
                    status="pending",
                    attempts=0,
                    available_at=datetime(2026, 4, 16, 10, 0, 0),
                ),
                AccountingSyncJob(
                    tenant_id=env["other_tenant_id"],
                    provider="quickbooks",
                    job_type="sync_customer",
                    entity_type="customer",
                    entity_id=customer_two.id,
                    status="pending",
                    attempts=0,
                    available_at=datetime(2026, 4, 16, 10, 0, 0),
                ),
            ]
        )
        db.commit()

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/query"):
            return httpx.Response(200, json={"QueryResponse": {}})
        if url.endswith("/customer"):
            return httpx.Response(
                200,
                json={
                    "Customer": {
                        "Id": "QB-ADMIN-RUN-1",
                        "SyncToken": "0",
                        "DisplayName": str((json or {}).get("DisplayName") or ""),
                    }
                },
            )
        raise AssertionError(f"Unexpected QuickBooks request: {method} {url}")

    import httpx

    monkeypatch.setattr(quickbooks_client_module.httpx, "request", fake_request)
    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        response = _post_with_csrf(client, "/admin/accounting/run-sync")
        assert response.status_code == 303
        assert "sync_run=1" in response.headers["location"]

        with env["SessionLocal"]() as db:
            acme_jobs = list(
                db.execute(
                    select(AccountingSyncJob).where(AccountingSyncJob.tenant_id == env["tenant_id"])
                ).scalars()
            )
            other_jobs = list(
                db.execute(
                    select(AccountingSyncJob).where(AccountingSyncJob.tenant_id == env["other_tenant_id"])
                ).scalars()
            )
            assert len(acme_jobs) == 1
            assert acme_jobs[0].status == "succeeded"
            assert len(other_jobs) == 1
            assert other_jobs[0].status == "pending"

        follow = client.get(response.headers["location"])
        assert follow.status_code == 200
        assert "Processed 1, succeeded 1, failed 0." in follow.text
        assert "qb-access-token" not in follow.text
        assert "qb-refresh-token" not in follow.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_tenant_admin_can_view_create_update_and_delete_tax_mappings(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-tax-manage",
    )
    tax_code_options = [
        ProviderTaxCodeOption(
            remote_tax_code_id="3",
            display_code="20.0% S",
            display_name="20.0% Standard Sales",
            description="20.0% Standard Sales",
            is_active=True,
            display_label="20.0% S - 20.0% Standard Sales",
        ),
        ProviderTaxCodeOption(
            remote_tax_code_id="2",
            display_code="Exempt From VAT",
            display_name="Exempt From VAT",
            description="Exempt From VAT",
            is_active=True,
            display_label="Exempt From VAT",
        ),
    ]
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(tax_code_options),
    )
    monkeypatch.setattr(
        tax_mapping_module,
        "list_provider_tax_codes",
        lambda *args, **kwargs: tax_code_options,
    )
    with env["SessionLocal"]() as db:
        tax_rate = TaxRate(
            code="VAT20-MANAGE",
            description="VAT 20 Manage",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(tax_rate)
        db.flush()
        db.add(
            Product(
                tenant_id=env["tenant_id"],
                code="MAP-PROD-1",
                description="Mapped Product",
                nominal_code="4000",
                unit_price=Decimal("15.00"),
                tax_rate_id=tax_rate.id,
            )
        )
        db.commit()
        tax_rate_id = int(tax_rate.id)

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")

        page = client.get("/admin/accounting/tax-mappings")
        assert page.status_code == 200
        assert "QuickBooks Tax Mappings" in page.text
        assert "VAT20-MANAGE" in page.text
        assert "20.0% S - 20.0% Standard Sales (Ref 3)" in page.text
        assert "Create Mapping" in page.text
        assert "No QuickBooks tax codes found in this company." not in page.text
        assert "qb-access-token" not in page.text
        assert "qb-refresh-token" not in page.text
        assert "qb-client-secret" not in page.text

        create_response = _post_with_csrf(
            client,
            "/admin/accounting/tax-mappings",
            data={
                "tax_rate_id": str(tax_rate_id),
                "name": "VAT 20 Sandbox",
                "external_id": "3",
                "is_active": "1",
            },
        )
        assert create_response.status_code == 303
        assert create_response.headers["location"] == "/admin/accounting/tax-mappings?tax_mapping_saved=1"

        tax_maps = _tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert len(tax_maps) == 1
        mapping_id = int(tax_maps[0].id)
        assert tax_maps[0].name == "VAT 20 Sandbox"
        assert tax_maps[0].external_id == "3"
        assert tax_maps[0].external_code == "20.0% S"
        assert tax_maps[0].is_active is True

        update_response = _post_with_csrf(
            client,
            f"/admin/accounting/tax-mappings/{mapping_id}/update",
            data={
                "name": "VAT 20 Sandbox Updated",
                "external_id": "2",
            },
        )
        assert update_response.status_code == 303
        assert update_response.headers["location"] == "/admin/accounting/tax-mappings?tax_mapping_saved=1"

        tax_maps = _tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert len(tax_maps) == 1
        assert tax_maps[0].name == "VAT 20 Sandbox Updated"
        assert tax_maps[0].external_id == "2"
        assert tax_maps[0].external_code == "Exempt From VAT"
        assert tax_maps[0].is_active is False

        delete_response = _post_with_csrf(
            client,
            f"/admin/accounting/tax-mappings/{mapping_id}/delete",
        )
        assert delete_response.status_code == 303
        assert delete_response.headers["location"] == "/admin/accounting/tax-mappings?tax_mapping_deleted=1"
        assert _tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"]) == []
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_tax_mapping_page_shows_clear_message_when_connected_company_has_no_tax_codes(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-tax-empty",
    )
    with env["SessionLocal"]() as db:
        tax_rate = TaxRate(
            code="VAT20-EMPTY",
            description="VAT 20 Empty",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(tax_rate)
        db.flush()
        db.add(
            Product(
                tenant_id=env["tenant_id"],
                code="MAP-PROD-EMPTY",
                description="Empty Tax Code Product",
                nominal_code="4000",
                unit_price=Decimal("15.00"),
                tax_rate_id=tax_rate.id,
            )
        )
        db.commit()
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(
            [],
            tax_rate_count=2,
            tax_agency_count=1,
        ),
    )

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        page = client.get("/admin/accounting/tax-mappings")
        assert page.status_code == 200
        assert "No QuickBooks tax codes found in this company." in page.text
        assert "QuickBooks returned 2 tax rates and 1 tax agency, but no TaxCode records for manual mapping." in page.text
        assert "Connect QuickBooks to load tax codes before creating a new mapping." not in page.text
        assert "Create Mapping" not in page.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_tax_mapping_page_shows_automated_vat_message_when_manual_mapping_is_not_required(
    tmp_path,
    monkeypatch,
):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-tax-auto",
    )
    with env["SessionLocal"]() as db:
        tax_rate = TaxRate(
            code="VAT20-AUTO",
            description="VAT 20 Auto",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(tax_rate)
        db.flush()
        db.add(
            Product(
                tenant_id=env["tenant_id"],
                code="MAP-PROD-AUTO",
                description="Automated VAT Product",
                nominal_code="4000",
                unit_price=Decimal("15.00"),
                tax_rate_id=tax_rate.id,
            )
        )
        db.commit()
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(
            [],
            partner_tax_enabled=True,
            company_country="GB",
            tax_rate_count=2,
            tax_agency_count=1,
        ),
    )

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        page = client.get("/admin/accounting/tax-mappings")
        assert page.status_code == 200
        assert "This QuickBooks company uses automated VAT. Manual tax mapping is not required." in page.text
        assert "No QuickBooks tax codes found in this company." not in page.text
        assert "Create Mapping" not in page.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_non_admin_and_platform_mode_cannot_manage_tax_mappings(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    with env["SessionLocal"]() as db:
        tax_rate = TaxRate(
            code="VAT20-PERMS",
            description="VAT 20 Permissions",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(tax_rate)
        db.flush()
        db.add(
            Product(
                tenant_id=env["tenant_id"],
                code="MAP-PROD-PERMS",
                description="Permissions Product",
                nominal_code="4000",
                unit_price=Decimal("15.00"),
                tax_rate_id=tax_rate.id,
            )
        )
        db.commit()
        tax_rate_id = int(tax_rate.id)

    operator_client = _client_for(env["app"], base_url="https://acme.localhost")
    platform_client = _client_for(env["app"], base_url="https://admin.localhost")
    try:
        _login(operator_client, email=env["operator_email"], password=env["password"], next_path="/")
        operator_page = operator_client.get("/admin/accounting/tax-mappings")
        assert operator_page.status_code == 403
        operator_create = _post_with_csrf(
            operator_client,
            "/admin/accounting/tax-mappings",
            data={
                "tax_rate_id": str(tax_rate_id),
                "external_id": "QB-DENIED-1",
                "external_code": "NON",
                "is_active": "1",
            },
        )
        assert operator_create.status_code == 403

        _login(
            platform_client,
            email="superadmin@example.com",
            password=env["password"],
            next_path="/platform/tenants",
        )
        platform_page = platform_client.get("/admin/accounting/tax-mappings")
        assert platform_page.status_code == 404
        platform_create = _post_with_csrf(
            platform_client,
            "/admin/accounting/tax-mappings",
            data={
                "tax_rate_id": str(tax_rate_id),
                "external_id": "QB-DENIED-2",
                "external_code": "NON",
                "is_active": "1",
            },
        )
        assert platform_create.status_code == 404
    finally:
        operator_client.close()
        platform_client.close()
        env["app"].dependency_overrides.clear()


def test_tax_mapping_management_preserves_tenant_isolation(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    tax_code_options = [
        ProviderTaxCodeOption(
            remote_tax_code_id="3",
            display_code="20.0% S",
            display_name="20.0% Standard Sales",
            description="20.0% Standard Sales",
            is_active=True,
            display_label="20.0% S - 20.0% Standard Sales",
        )
    ]
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(tax_code_options),
    )
    monkeypatch.setattr(
        tax_mapping_module,
        "list_provider_tax_codes",
        lambda *args, **kwargs: tax_code_options,
    )
    with env["SessionLocal"]() as db:
        shared_rate = TaxRate(
            code="VAT20-ISOLATION",
            description="VAT 20 Isolation",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(shared_rate)
        db.flush()
        db.add_all(
            [
                Product(
                    tenant_id=env["tenant_id"],
                    code="MAP-PROD-ISO-1",
                    description="Isolation Product One",
                    nominal_code="4000",
                    unit_price=Decimal("15.00"),
                    tax_rate_id=shared_rate.id,
                ),
                Product(
                    tenant_id=env["other_tenant_id"],
                    code="MAP-PROD-ISO-2",
                    description="Isolation Product Two",
                    nominal_code="4000",
                    unit_price=Decimal("15.00"),
                    tax_rate_id=shared_rate.id,
                ),
            ]
        )
        db.flush()
        acme_map = AccountingTaxMap(
            tenant_id=env["tenant_id"],
            provider="quickbooks",
            tax_rate_id=shared_rate.id,
            external_id="QB-ISO-ACME",
            external_code="TAX",
            is_active=True,
        )
        db.add(acme_map)
        db.commit()
        mapping_id = int(acme_map.id)

    other_client = _client_for(env["app"], base_url="https://other.localhost")
    try:
        _login(
            other_client,
            email=env["other_admin_email"],
            password=env["password"],
            next_path="/admin",
        )
        page = other_client.get("/admin/accounting/tax-mappings")
        assert page.status_code == 200
        assert "VAT20-ISOLATION" in page.text
        assert "QB-ISO-ACME" not in page.text

        update_response = _post_with_csrf(
            other_client,
            f"/admin/accounting/tax-mappings/{mapping_id}/update",
            data={
                "name": "Other Tenant Hijack",
                "external_id": "3",
                "is_active": "1",
            },
        )
        assert update_response.status_code == 303
        assert "QuickBooks tax mapping was not found." in unquote_plus(update_response.headers["location"])

        delete_response = _post_with_csrf(
            other_client,
            f"/admin/accounting/tax-mappings/{mapping_id}/delete",
        )
        assert delete_response.status_code == 303
        assert "QuickBooks tax mapping was not found." in unquote_plus(delete_response.headers["location"])

        tax_maps = _tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"])
        assert len(tax_maps) == 1
        assert tax_maps[0].external_id == "QB-ISO-ACME"
        assert _tax_maps_for_tenant(env["SessionLocal"], env["other_tenant_id"]) == []
    finally:
        other_client.close()
        env["app"].dependency_overrides.clear()


def test_tax_mapping_duplicate_and_invalid_scope_errors_are_reported_cleanly(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-tax-errors",
    )
    tax_code_options = [
        ProviderTaxCodeOption(
            remote_tax_code_id="3",
            display_code="20.0% S",
            display_name="20.0% Standard Sales",
            description="20.0% Standard Sales",
            is_active=True,
            display_label="20.0% S - 20.0% Standard Sales",
        ),
        ProviderTaxCodeOption(
            remote_tax_code_id="2",
            display_code="Exempt From VAT",
            display_name="Exempt From VAT",
            description="Exempt From VAT",
            is_active=True,
            display_label="Exempt From VAT",
        ),
    ]
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(tax_code_options),
    )
    monkeypatch.setattr(
        tax_mapping_module,
        "list_provider_tax_codes",
        lambda *args, **kwargs: tax_code_options,
    )
    with env["SessionLocal"]() as db:
        mapped_rate = TaxRate(
            code="VAT20-DUPE-A",
            description="VAT 20 Duplicate A",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        second_rate = TaxRate(
            code="VAT20-DUPE-B",
            description="VAT 20 Duplicate B",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        unused_rate = TaxRate(
            code="VAT20-UNUSED",
            description="VAT 20 Unused",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add_all([mapped_rate, second_rate, unused_rate])
        db.flush()
        db.add_all(
            [
                Product(
                    tenant_id=env["tenant_id"],
                    code="MAP-PROD-DUPE-1",
                    description="Duplicate Product One",
                    nominal_code="4000",
                    unit_price=Decimal("10.00"),
                    tax_rate_id=mapped_rate.id,
                ),
                Product(
                    tenant_id=env["tenant_id"],
                    code="MAP-PROD-DUPE-2",
                    description="Duplicate Product Two",
                    nominal_code="4000",
                    unit_price=Decimal("10.00"),
                    tax_rate_id=second_rate.id,
                ),
                AccountingTaxMap(
                    tenant_id=env["tenant_id"],
                    provider="quickbooks",
                    tax_rate_id=mapped_rate.id,
                    external_id="3",
                    external_code="20.0% S",
                    is_active=True,
                ),
            ]
        )
        db.commit()
        second_rate_id = int(second_rate.id)
        unused_rate_id = int(unused_rate.id)

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")

        duplicate_response = _post_with_csrf(
            client,
            "/admin/accounting/tax-mappings",
            data={
                "tax_rate_id": str(second_rate_id),
                "external_id": "3",
                "is_active": "1",
            },
        )
        assert duplicate_response.status_code == 303
        assert "already used by another local tax mapping" in unquote_plus(
            duplicate_response.headers["location"]
        )
        assert len(_tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"])) == 1

        invalid_scope_response = _post_with_csrf(
            client,
            "/admin/accounting/tax-mappings",
            data={
                "tax_rate_id": str(unused_rate_id),
                "external_id": "2",
                "is_active": "1",
            },
        )
        assert invalid_scope_response.status_code == 303
        assert "not currently used by this tenant's products" in unquote_plus(
            invalid_scope_response.headers["location"]
        )
        assert len(_tax_maps_for_tenant(env["SessionLocal"], env["tenant_id"])) == 1
    finally:
        client.close()
        env["app"].dependency_overrides.clear()


def test_tax_mapping_page_flags_legacy_display_value_refs(tmp_path, monkeypatch):
    env = _prepare_environment(tmp_path, monkeypatch)
    _seed_connected_connection(
        env["SessionLocal"],
        tenant_id=env["tenant_id"],
        realm_id="realm-tax-legacy",
    )
    tax_code_options = [
        ProviderTaxCodeOption(
            remote_tax_code_id="3",
            display_code="20.0% S",
            display_name="20.0% Standard Sales",
            description="20.0% Standard Sales",
            is_active=True,
            display_label="20.0% S - 20.0% Standard Sales",
        )
    ]
    monkeypatch.setattr(
        admin_accounting_route,
        "inspect_quickbooks_tax_discovery",
        lambda *args, **kwargs: _tax_discovery_state(tax_code_options),
    )
    monkeypatch.setattr(
        tax_mapping_module,
        "list_provider_tax_codes",
        lambda *args, **kwargs: tax_code_options,
    )
    with env["SessionLocal"]() as db:
        tax_rate = TaxRate(
            code="VAT20-LEGACY",
            description="VAT 20 Legacy",
            rate_percent=Decimal("20.000"),
            is_active=True,
        )
        db.add(tax_rate)
        db.flush()
        db.add(
            Product(
                tenant_id=env["tenant_id"],
                code="MAP-PROD-LEGACY",
                description="Legacy Tax Product",
                nominal_code="4000",
                unit_price=Decimal("10.00"),
                tax_rate_id=tax_rate.id,
            )
        )
        db.add(
            AccountingTaxMap(
                tenant_id=env["tenant_id"],
                provider="quickbooks",
                tax_rate_id=tax_rate.id,
                external_id="20.0% S",
                external_code="20.0% S",
                name="Legacy VAT 20",
                is_active=True,
            )
        )
        db.commit()

    client = _client_for(env["app"], base_url="https://acme.localhost")
    try:
        _login(client, email=env["admin_email"], password=env["password"], next_path="/admin")
        page = client.get("/admin/accounting/tax-mappings")
        assert page.status_code == 200
        assert "display code/label, not the provider ref used for sync" in page.text
        assert "Re-save this mapping from the QuickBooks tax list" in page.text
    finally:
        client.close()
        env["app"].dependency_overrides.clear()
