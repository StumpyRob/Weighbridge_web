from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.auth import (
    ROLE_ACCOUNTS,
    ROLE_OPERATOR,
    ROLE_READ_ONLY,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    hash_password,
    user_identity_kwargs,
)
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import AuditEvent, Base, Customer, Invoice, Tenant, Ticket, User, UserFeedback
from app.models.base import utcnow
from app.models.ticket import DirectionEnum, TicketStatusEnum, TransactionTypeEnum
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.seed import seed_print_destinations, seed_print_templates
from app.services.email_service import EmailSendResult
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
    monkeypatch.setattr(settings, "app_secret_key", "tenant-role-test-secret")
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


def _seed_tenant(SessionLocal: sessionmaker, *, name: str, subdomain: str, ai_enabled: bool) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=True, ai_enabled=ai_enabled)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _seed_tenant_baseline(SessionLocal: sessionmaker, *, tenant_id: int, company_name: str) -> None:
    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_id
        db.info["platform_mode"] = False
        company = ensure_company_settings_row_exists(db)
        company.tenant_id = tenant_id
        company.name = company_name
        company.is_initialized = True
        upsert_default_yard(db, yard_name=DEFAULT_YARD_NAME)
        seed_required_reference_data(db)
        seed_print_templates(db)
        seed_print_destinations(db)
        db.commit()


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    email: str,
    password: str,
    role: str,
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


def _seed_customer_ticket_invoice(SessionLocal: sessionmaker, *, tenant_id: int) -> tuple[int, int, int]:
    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_id
        db.info["platform_mode"] = False
        customer = Customer(account_code="CUST001", name="Acme Recycling")
        db.add(customer)
        db.flush()

        ticket = Ticket(
            tenant_id=tenant_id,
            ticket_no="T-0001",
            datetime=utcnow(),
            status=TicketStatusEnum.OPEN.value,
            direction=DirectionEnum.INWARD.value,
            transaction_type=TransactionTypeEnum.WASTEIN.value,
            customer_id=customer.id,
            dont_invoice=False,
            paid=False,
        )
        db.add(ticket)
        db.flush()

        invoice = Invoice(
            tenant_id=tenant_id,
            invoice_no="INV-0001",
            customer_id=customer.id,
            invoice_date=date.today(),
            due_date=date.today(),
            status="DRAFT",
            net_total=Decimal("10.00"),
            vat_total=Decimal("2.00"),
            gross_total=Decimal("12.00"),
        )
        db.add(invoice)
        db.commit()
        db.refresh(customer)
        db.refresh(ticket)
        db.refresh(invoice)
        return int(customer.id), int(ticket.id), int(invoice.id)


def _seed_feedback(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    title: str,
    message: str,
    kind: str = "bug",
    status: str = "new",
    reporter_name: str = "Reporter",
    reporter_email: str = "reporter@example.com",
) -> int:
    with SessionLocal() as db:
        db.info["platform_mode"] = True
        feedback = UserFeedback(
            tenant_id=tenant_id,
            kind=kind,
            status=status,
            title=title,
            message=message,
            source_path="/tickets",
            source_title="Tickets",
            submitted_by_display_name=reporter_name,
            submitted_by_email=reporter_email,
            host_name="acme.localhost",
            email_delivery_status="sent",
            recipient_email="dev@example.com",
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)
        return int(feedback.id)


def _prime_csrf(client: TestClient, *, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    client.headers.update({CSRF_HEADER_NAME: token})
    return token


def _login(client: TestClient, *, email: str, password: str, next_path: str = "/") -> str:
    csrf = _prime_csrf(client, path="/login")
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


@pytest.fixture()
def workspace_env(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="tenant-permissions.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme", ai_enabled=True)
    _seed_tenant_baseline(SessionLocal, tenant_id=tenant_id, company_name="Acme")
    customer_id, ticket_id, invoice_id = _seed_customer_ticket_invoice(
        SessionLocal,
        tenant_id=tenant_id,
    )
    password = "TestPass123!"
    users = {
        "tenant_admin": {
            "id": _seed_user(
                SessionLocal,
                tenant_id=tenant_id,
                email="admin@acme.example",
                password=password,
                role=ROLE_TENANT_ADMIN,
                first_name="Tina",
                last_name="Admin",
            ),
            "email": "admin@acme.example",
        },
        "operator": {
            "id": _seed_user(
                SessionLocal,
                tenant_id=tenant_id,
                email="operator@acme.example",
                password=password,
                role=ROLE_OPERATOR,
                first_name="Olivia",
                last_name="Operator",
            ),
            "email": "operator@acme.example",
        },
        "accounts": {
            "id": _seed_user(
                SessionLocal,
                tenant_id=tenant_id,
                email="accounts@acme.example",
                password=password,
                role=ROLE_ACCOUNTS,
                first_name="Ava",
                last_name="Accounts",
            ),
            "email": "accounts@acme.example",
        },
        "read_only": {
            "id": _seed_user(
                SessionLocal,
                tenant_id=tenant_id,
                email="readonly@acme.example",
                password=password,
                role=ROLE_READ_ONLY,
                first_name="Rory",
                last_name="Readonly",
            ),
            "email": "readonly@acme.example",
        },
    }
    yield {
        "app": app,
        "SessionLocal": SessionLocal,
        "base_url": "https://acme.localhost",
        "tenant_id": tenant_id,
        "password": password,
        "users": users,
        "customer_id": customer_id,
        "ticket_id": ticket_id,
        "invoice_id": invoice_id,
    }
    app.dependency_overrides.clear()


def _client_for_role(env, role_key: str) -> tuple[TestClient, str]:
    client = TestClient(env["app"], base_url=env["base_url"])
    user = env["users"][role_key]
    csrf = _login(client, email=user["email"], password=env["password"])
    return client, csrf


def test_email_login_shows_full_name_and_role_label(workspace_env):
    client, _csrf = _client_for_role(workspace_env, "operator")
    try:
        response = client.get("/")
        assert response.status_code == 200
        assert "Signed in as Olivia Operator" in response.text
        assert "Operator" in response.text
    finally:
        client.close()


def test_shared_sidebar_shell_renders_major_tenant_pages(workspace_env):
    client, _csrf = _client_for_role(workspace_env, "tenant_admin")
    ticket_id = workspace_env["ticket_id"]
    page_cases = [
        ("/", "Home"),
        ("/tickets", "Tickets"),
        (f"/tickets/{ticket_id}", "Tickets"),
        ("/customers", "Customers"),
        ("/vehicles", "Vehicles"),
        ("/products", "Products"),
        ("/invoices", "Invoices"),
        ("/lookups/hauliers", "Lookups"),
        ("/reports", "Reports"),
        ("/admin", "Settings"),
    ]

    try:
        for path, active_label in page_cases:
            response = client.get(path)
            assert response.status_code == 200, path
            assert 'class="site-header"' in response.text
            assert 'id="site-sidebar"' in response.text
            assert 'data-shell-toggle' in response.text
            assert 'data-shell-backdrop' in response.text
            assert '<nav class="site-nav">' in response.text
            assert '<script src="/static/js/app_shell.js?v=' in response.text
            assert f'aria-current="page">{active_label}<' in response.text
            assert 'href="/help"' in response.text
            assert 'class="site-sidebar-help' in response.text
            assert 'class="site-sidebar-feedback"' in response.text
            assert 'class="site-sidebar-build"' in response.text
            assert "Build: v" in response.text
            assert 'data-feedback-dialog' in response.text
            assert '<script src="/static/js/feedback_dialog.js?v=' in response.text
    finally:
        client.close()


def test_shared_help_page_is_available_to_non_admin_users(workspace_env):
    client, _csrf = _client_for_role(workspace_env, "operator")
    try:
        getting_started = client.get("/help/getting-started")
        assert getting_started.status_code == 200
        assert "Daily Workflow (Operators)" in getting_started.text
        assert "Back to Home" in getting_started.text

        template_variables = client.get("/help/template-variables")
        assert template_variables.status_code == 403
    finally:
        client.close()


def test_superadmin_feedback_inbox_is_cross_tenant_and_supports_status_updates(
    workspace_env,
    monkeypatch,
):
    operator_client, operator_csrf = _client_for_role(workspace_env, "operator")
    tenant_admin_client, _tenant_admin_csrf = _client_for_role(workspace_env, "tenant_admin")
    platform_client = TestClient(workspace_env["app"], base_url="https://admin.localhost")
    SessionLocal = workspace_env["SessionLocal"]
    sent: dict[str, object] = {}

    def _fake_send_email(**kwargs):
        sent.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(settings, "developer_feedback_email", "dev@example.com")
    monkeypatch.setattr("app.routes.account.send_email", _fake_send_email)

    superadmin_id = _seed_user(
        SessionLocal,
        tenant_id=None,
        email="superadmin@example.com",
        password=workspace_env["password"],
        role=ROLE_SUPERADMIN,
        first_name="Pat",
        last_name="Platform",
    )
    other_tenant_id = _seed_tenant(
        SessionLocal,
        name="Other Workspace",
        subdomain="other",
        ai_enabled=False,
    )
    _seed_feedback(
        SessionLocal,
        tenant_id=other_tenant_id,
        title="Other tenant issue",
        message="This should only appear in the platform inbox.",
        reporter_name="Other Reporter",
        reporter_email="other@example.com",
    )

    try:
        submit = operator_client.post(
            "/feedback",
            data={
                "kind": "wish",
                "title": "Add a quicker invoice shortcut",
                "message": "A quicker invoice shortcut would help in the office.",
                "source_path": "/invoices",
                "source_title": "Invoices | Weighbridge Web",
                CSRF_FORM_FIELD: operator_csrf,
            },
            follow_redirects=False,
        )
        assert submit.status_code == 303

        with SessionLocal() as db:
            feedback = (
                db.execute(
                    select(UserFeedback)
                    .where(
                        UserFeedback.tenant_id == workspace_env["tenant_id"],
                        UserFeedback.title == "Add a quicker invoice shortcut",
                    )
                    .order_by(UserFeedback.id.desc())
                    .limit(1)
                )
                .scalars()
                .one()
            )
            feedback_id = int(feedback.id)

        tenant_settings = tenant_admin_client.get("/admin")
        assert tenant_settings.status_code == 200
        assert "Feedback Inbox" not in tenant_settings.text
        assert "Open Feedback" not in tenant_settings.text
        assert tenant_admin_client.get("/admin/feedback").status_code == 404

        platform_csrf = _login(
            platform_client,
            email="superadmin@example.com",
            password=workspace_env["password"],
            next_path="/platform/tenants",
        )

        tenants_page = platform_client.get("/platform/tenants")
        assert tenants_page.status_code == 200
        assert "Feedback" in tenants_page.text
        assert "Open Feedback" in tenants_page.text
        assert "2 new" in tenants_page.text

        inbox = platform_client.get("/platform/feedback")
        assert inbox.status_code == 200
        assert "Feedback Inbox" in inbox.text
        assert "Add a quicker invoice shortcut" in inbox.text
        assert "Other tenant issue" in inbox.text
        assert "Acme" in inbox.text
        assert "Other Workspace" in inbox.text
        assert "Feature request" in inbox.text
        assert "Sent" in inbox.text
        assert ">Feedback<" in inbox.text

        filtered_inbox = platform_client.get(f"/platform/feedback?tenant_id={workspace_env['tenant_id']}")
        assert filtered_inbox.status_code == 200
        assert "Add a quicker invoice shortcut" in filtered_inbox.text
        assert "Other tenant issue" not in filtered_inbox.text

        update = platform_client.post(
            f"/platform/feedback/{feedback_id}/status",
            data={
                "status": "reviewed",
                "return_to": "/platform/feedback",
                CSRF_FORM_FIELD: platform_csrf,
            },
            follow_redirects=False,
        )
        assert update.status_code == 303
        assert (
            update.headers["location"]
            == "/platform/feedback?feedback_updated=1&feedback_update_status=reviewed"
        )

        with SessionLocal() as db:
            refreshed = db.get(UserFeedback, feedback_id)
            assert refreshed is not None
            assert refreshed.status == "reviewed"
            assert refreshed.reviewed_by_user_id == superadmin_id
            assert refreshed.reviewed_at is not None

            event = (
                db.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id.is_(None),
                        AuditEvent.action == "USER_FEEDBACK_STATUS_UPDATE",
                        AuditEvent.entity_type == "user_feedback",
                        AuditEvent.entity_id == str(feedback_id),
                    )
                    .order_by(AuditEvent.id.desc())
                    .limit(1)
                )
                .scalars()
                .one()
            )
            details = event.details_json or {}
            assert details.get("workspace") == "Acme"
            assert details.get("tenant_id") == workspace_env["tenant_id"]
            assert details.get("status", {}).get("from") == "new"
            assert details.get("status", {}).get("to") == "reviewed"
    finally:
        operator_client.close()
        tenant_admin_client.close()
        platform_client.close()


def test_workspace_user_can_submit_sidebar_feedback_and_write_audit(
    workspace_env,
    monkeypatch,
):
    client, csrf = _client_for_role(workspace_env, "operator")
    SessionLocal = workspace_env["SessionLocal"]
    sent: dict[str, object] = {}

    def _fake_send_email(**kwargs):
        sent.update(kwargs)
        return EmailSendResult(ok=True)

    monkeypatch.setattr(settings, "developer_feedback_email", "dev@example.com")
    monkeypatch.setattr("app.routes.account.send_email", _fake_send_email)

    try:
        response = client.post(
            "/feedback",
            data={
                "kind": "bug",
                "title": "Sidebar overlap on tickets",
                "message": "The sidebar overlaps the content on the tickets list.",
                "source_path": "/tickets",
                "source_title": "Tickets | Weighbridge Web",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/tickets?feedback_sent=1&feedback_kind=bug"

        with SessionLocal() as db:
            feedback = (
                db.execute(
                    select(UserFeedback)
                    .where(UserFeedback.tenant_id == workspace_env["tenant_id"])
                    .order_by(UserFeedback.id.desc())
                    .limit(1)
                )
                .scalars()
                .one()
            )
            feedback_id = int(feedback.id)
            assert feedback.kind == "bug"
            assert feedback.status == "new"
            assert feedback.title == "Sidebar overlap on tickets"
            assert feedback.message == "The sidebar overlaps the content on the tickets list."
            assert feedback.email_delivery_status == "sent"
            assert feedback.recipient_email == "dev@example.com"
            assert feedback.submitted_by_display_name == "Olivia Operator"
            assert feedback.source_path == "/tickets"

        assert sent["to"] == ["dev@example.com"]
        assert sent["db"] is not None
        assert sent["subject"] == f"[Bug report] Acme: #{feedback_id} Sidebar overlap on tickets"
        assert "Workspace: Acme" in str(sent["text_body"])
        assert f"Feedback ID: {feedback_id}" in str(sent["text_body"])
        assert "User: Olivia Operator" in str(sent["text_body"])
        assert "Role: Operator" in str(sent["text_body"])
        assert "Page: /tickets" in str(sent["text_body"])
        assert "The sidebar overlaps the content on the tickets list." in str(sent["text_body"])

        with SessionLocal() as db:
            event = (
                db.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == workspace_env["tenant_id"],
                        AuditEvent.action == "USER_FEEDBACK_SUBMIT",
                        AuditEvent.entity_type == "user_feedback",
                    )
                    .order_by(AuditEvent.id.desc())
                    .limit(1)
                )
                .scalars()
                .one()
            )
            details = event.details_json or {}
            assert details.get("feedback_id") == feedback_id
            assert details.get("kind") == "bug"
            assert details.get("title") == "Sidebar overlap on tickets"
            assert details.get("source_path") == "/tickets"
            assert details.get("workspace") == "Acme"
            assert details.get("recipient") == "dev@example.com"
            assert details.get("status") == "sent"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("role_key", "expected"),
    [
        (
            "tenant_admin",
            {
                "/": 200,
                "/tickets": 200,
                "/invoices": 200,
                "/admin/users": 200,
                "/admin/company": 200,
            },
        ),
        (
            "operator",
            {
                "/": 200,
                "/tickets": 200,
                "/customers": 200,
                "/admin/users": 403,
                "/admin/company": 403,
                "/invoices": 403,
            },
        ),
        (
            "accounts",
            {
                "/": 200,
                "/tickets": 200,
                "/invoices": 200,
                "/admin/users": 403,
                "/admin/company": 403,
            },
        ),
        (
            "read_only",
            {
                "/": 200,
                "/tickets": 200,
                "/invoices": 200,
                "/admin/users": 403,
                "/admin/company": 403,
            },
        ),
    ],
)
def test_role_access_is_limited_to_v1_surfaces(workspace_env, role_key, expected):
    client, _csrf = _client_for_role(workspace_env, role_key)
    try:
        for path, status_code in expected.items():
            response = client.get(path)
            assert response.status_code == status_code, f"{role_key} unexpected access for {path}"
    finally:
        client.close()


def test_read_only_cannot_use_document_email_actions(workspace_env):
    client, csrf = _client_for_role(workspace_env, "read_only")
    SessionLocal = workspace_env["SessionLocal"]
    ticket_id = workspace_env["ticket_id"]
    invoice_id = workspace_env["invoice_id"]

    with SessionLocal() as db:
        db.info["tenant_id"] = workspace_env["tenant_id"]
        db.info["platform_mode"] = False
        ticket = db.get(Ticket, ticket_id)
        assert ticket is not None
        ticket.status = TicketStatusEnum.COMPLETE.value
        db.commit()

    try:
        invoice_page = client.get(f"/invoices/{invoice_id}")
        assert invoice_page.status_code == 200
        assert "Email Invoice" not in invoice_page.text

        ticket_page = client.get(f"/tickets/{ticket_id}")
        assert ticket_page.status_code == 200
        assert "Email Ticket" not in ticket_page.text

        invoice_response = client.post(
            f"/invoices/{invoice_id}/email",
            data={
                "to_email": "readonly@example.com",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert invoice_response.status_code == 403

        ticket_response = client.post(
            f"/tickets/{ticket_id}/email",
            data={
                "to_email": "readonly@example.com",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert ticket_response.status_code == 403
    finally:
        client.close()


def test_backend_rejects_forbidden_posts_and_read_only_ai(workspace_env):
    operator_client, operator_csrf = _client_for_role(workspace_env, "operator")
    accounts_client, accounts_csrf = _client_for_role(workspace_env, "accounts")
    readonly_client, readonly_csrf = _client_for_role(workspace_env, "read_only")
    try:
        operator_response = operator_client.post(
            "/admin/users",
            data={
                "first_name": "Blocked",
                "last_name": "User",
                "email": "blocked@acme.example",
                "role": ROLE_OPERATOR,
                "password": "AnotherPass123!",
                "confirm_password": "AnotherPass123!",
                CSRF_FORM_FIELD: operator_csrf,
            },
            follow_redirects=False,
        )
        assert operator_response.status_code == 403

        accounts_response = accounts_client.post(
            "/tickets/new/quick",
            data={CSRF_FORM_FIELD: accounts_csrf},
            follow_redirects=False,
        )
        assert accounts_response.status_code == 403

        readonly_response = readonly_client.post(
            "/customers/new",
            data={
                "account_code": "BLOCK1",
                "name": "Blocked Customer",
                CSRF_FORM_FIELD: readonly_csrf,
            },
            follow_redirects=False,
        )
        assert readonly_response.status_code == 403

        readonly_home = readonly_client.get("/")
        assert readonly_home.status_code == 200
        assert "data-assistant-open" not in readonly_home.text

        readonly_ai = readonly_client.post(
            "/api/assistant/query",
            json={"question": "What is open today?"},
            headers={CSRF_HEADER_NAME: readonly_csrf},
        )
        assert readonly_ai.status_code == 403
    finally:
        operator_client.close()
        accounts_client.close()
        readonly_client.close()


def test_tenant_admin_can_manage_workspace_users_and_audit_actions(workspace_env):
    client, csrf = _client_for_role(workspace_env, "tenant_admin")
    SessionLocal = workspace_env["SessionLocal"]
    operator_id = workspace_env["users"]["operator"]["id"]
    try:
        page = client.get("/admin/users")
        assert page.status_code == 200
        assert "Create User" in page.text
        assert "Name" in page.text
        assert "Email" in page.text
        assert "Role" in page.text
        assert "Status" in page.text
        assert "Actions" in page.text
        assert "First name" in page.text
        assert "Last name" in page.text
        assert "Tenant Admin" in page.text
        assert "Operator" in page.text
        assert "Accounts" in page.text
        assert "Read Only" in page.text
        assert f"/admin/users?edit_user={operator_id}" in page.text
        assert f"/admin/users?reset_user={operator_id}" in page.text
        assert f'id="reset_user_{operator_id}_password"' not in page.text
        assert f'id="edit_user_{operator_id}_first_name"' not in page.text

        edit_page = client.get(f"/admin/users?edit_user={operator_id}")
        assert edit_page.status_code == 200
        assert f'id="edit_user_{operator_id}_first_name"' in edit_page.text
        assert f'id="edit_user_{operator_id}_last_name"' in edit_page.text
        assert f'id="edit_user_{operator_id}_email"' in edit_page.text
        assert f'id="edit_user_{operator_id}_role"' in edit_page.text

        reset_page = client.get(f"/admin/users?reset_user={operator_id}")
        assert reset_page.status_code == 200
        assert f'id="reset_user_{operator_id}_password"' in reset_page.text
        assert f'id="reset_user_{operator_id}_confirm_password"' in reset_page.text

        create_response = client.post(
            "/admin/users",
            data={
                "first_name": "Nina",
                "last_name": "Newuser",
                "email": "nina@acme.example",
                "role": ROLE_OPERATOR,
                "password": "AnotherPass123!",
                "confirm_password": "AnotherPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert create_response.status_code == 303

        with SessionLocal() as db:
            user = db.execute(
                select(User).where(User.tenant_id == workspace_env["tenant_id"], User.username == "nina@acme.example")
            ).scalar_one()
            user_id = int(user.id)
            assert user.email == "nina@acme.example"
            assert user.first_name == "Nina"
            assert user.last_name == "Newuser"
            assert user.full_name == "Nina Newuser"
            assert user.role == ROLE_OPERATOR

        update_response = client.post(
            f"/admin/users/{user_id}/update",
            data={
                "first_name": "Nina",
                "last_name": "Newuser",
                "email": "nina@acme.example",
                "role": ROLE_ACCOUNTS,
                "is_active": "off",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert update_response.status_code == 303

        reset_response = client.post(
            f"/admin/users/{user_id}/reset-password",
            data={
                "password": "ResetPass123!",
                "confirm_password": "ResetPass123!",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert reset_response.status_code == 303

        with SessionLocal() as db:
            refreshed = db.get(User, user_id)
            assert refreshed is not None
            assert refreshed.role == ROLE_ACCOUNTS
            assert not refreshed.is_active
            actions = {
                row.action
                for row in db.execute(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == workspace_env["tenant_id"],
                        AuditEvent.entity_type == "user",
                        AuditEvent.entity_id == user_id,
                    )
                ).scalars()
            }
            assert "USER_CREATE" in actions
            assert "USER_ROLE_CHANGE" in actions
            assert "USER_DEACTIVATE" in actions
            assert "USER_PASSWORD_RESET" in actions
    finally:
        client.close()


def test_cannot_remove_last_active_tenant_admin(workspace_env):
    client, csrf = _client_for_role(workspace_env, "tenant_admin")
    admin_id = workspace_env["users"]["tenant_admin"]["id"]
    SessionLocal = workspace_env["SessionLocal"]
    try:
        response = client.post(
            f"/admin/users/{admin_id}/update",
            data={
                "first_name": "Tina",
                "last_name": "Admin",
                "email": workspace_env["users"]["tenant_admin"]["email"],
                "role": ROLE_OPERATOR,
                "is_active": "on",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        follow = client.get(response.headers["location"])
        assert follow.status_code == 200
        assert "You must keep at least one active Tenant Admin in the workspace." in follow.text

        with SessionLocal() as db:
            admin = db.get(User, admin_id)
            assert admin is not None
            assert admin.role == ROLE_TENANT_ADMIN
            assert admin.is_active is True
    finally:
        client.close()


def test_ai_insights_visible_for_active_read_only_user(workspace_env, monkeypatch):
    client, _csrf = _client_for_role(workspace_env, "read_only")
    monkeypatch.setattr(
        main_module,
        "generate_dashboard_insights",
        lambda *args, **kwargs: {"items": ["Throughput is up today."], "message": ""},
    )
    try:
        response = client.get("/")
        assert response.status_code == 200
        assert "AI Insights" in response.text
        assert "Throughput is up today." in response.text
    finally:
        client.close()
