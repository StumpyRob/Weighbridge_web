from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import (
    ROLE_OPERATOR,
    ROLE_TENANT_ADMIN,
    hash_password,
    user_identity_kwargs,
)
from app.config import settings
from app.db import TenantSession, get_db
from app.main import create_app
from app.models import (
    Base,
    PrintDestination,
    PrintTemplate,
    Tenant,
    User,
    WorkstationPrinterProfile,
)
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, CSRF_HEADER_NAME
from app.tenancy import current_platform_mode, current_tenant_id


def _build_app_and_session(
    tmp_path: Path,
    *,
    db_name: str,
    monkeypatch,
) -> tuple[FastAPI, sessionmaker]:
    monkeypatch.setattr(settings, "app_secret_key", "qz-workstation-test-secret")
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


def _prime_csrf(client: TestClient, *, path: str = "/login") -> str:
    response = client.get(path)
    assert response.status_code in {200, 302, 303}
    token = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert token
    client.headers.update({CSRF_HEADER_NAME: token})
    return token


def _login(client: TestClient, *, email: str, password: str, next_path: str) -> None:
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
    refreshed = str(client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert refreshed
    client.headers.update({CSRF_HEADER_NAME: refreshed})


def _seed_tenant(SessionLocal: sessionmaker, *, name: str, subdomain: str) -> int:
    with SessionLocal() as db:
        tenant = Tenant(name=name, subdomain=subdomain, is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        return int(tenant.id)


def _seed_user(
    SessionLocal: sessionmaker,
    *,
    email: str,
    password: str,
    role: str,
    tenant_id: int,
) -> None:
    with SessionLocal() as db:
        db.info["tenant_id"] = tenant_id
        db.info["platform_mode"] = False
        db.add(
            User(
                **user_identity_kwargs(email=email, role=role),
                password_hash=hash_password(password),
                is_active=True,
                tenant_id=tenant_id,
            )
        )
        db.commit()


def _tenant_db(SessionLocal: sessionmaker, tenant_id: int):
    db = SessionLocal()
    db.info["tenant_id"] = tenant_id
    db.info["platform_mode"] = False
    return db


def _create_destination(
    SessionLocal: sessionmaker,
    *,
    tenant_id: int,
    document_type: str,
    printer_name: str | None,
) -> None:
    with _tenant_db(SessionLocal, tenant_id) as db:
        template = PrintTemplate(
            code=f"{document_type}_QZ_TEMPLATE",
            description=f"{document_type} QZ Template",
            document_type=document_type,
            format="HTML",
            content=f"<html><body>{document_type}</body></html>",
            is_active=True,
        )
        db.add(template)
        db.flush()
        delivery_config: dict[str, object] = {"qz_direct_print_enabled": True}
        if printer_name:
            delivery_config["qz_printer_name"] = printer_name
        db.add(
            PrintDestination(
                name=f"{document_type} QZ Destination",
                description=f"{document_type} QZ Destination",
                document_type=document_type,
                template_id=template.id,
                delivery_type="PRINT_LOCAL_BROWSER",
                delivery_config=delivery_config,
                is_default=True,
                is_active=True,
            )
        )
        db.commit()


def test_qz_workstation_register_creates_placeholder_profiles_for_all_document_types(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="qz-workstation-register.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_user(
        SessionLocal,
        email="operator@example.com",
        password="TestPass123!",
        role=ROLE_OPERATOR,
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(client, email="operator@example.com", password="TestPass123!", next_path="/")
        response = client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "ws-front-desk"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workstation"]["key"] == "ws-front-desk"
    assert payload["needs_workstation_name"] is True

    with _tenant_db(SessionLocal, tenant_id) as db:
        rows = list(
            db.execute(
                select(WorkstationPrinterProfile).order_by(
                    WorkstationPrinterProfile.document_type.asc()
                )
            ).scalars()
        )

    assert [row.document_type for row in rows] == ["INVOICE", "TICKET", "WTN"]
    assert all(row.workstation_key == "ws-front-desk" for row in rows)
    assert all(bool(row.is_active) is False for row in rows)
    assert all((row.printer_name or "") == "" for row in rows)


def test_qz_workstation_resolution_order_prefers_workstation_then_destination_then_default(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="qz-workstation-resolution.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_user(
        SessionLocal,
        email="operator@example.com",
        password="TestPass123!",
        role=ROLE_OPERATOR,
        tenant_id=tenant_id,
    )
    _create_destination(
        SessionLocal,
        tenant_id=tenant_id,
        document_type="TICKET",
        printer_name="Destination Ticket Printer",
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(client, email="operator@example.com", password="TestPass123!", next_path="/")
        client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "ws-ticket"},
        )

        with _tenant_db(SessionLocal, tenant_id) as db:
            ticket_profile = db.execute(
                select(WorkstationPrinterProfile).where(
                    WorkstationPrinterProfile.workstation_key == "ws-ticket",
                    WorkstationPrinterProfile.document_type == "TICKET",
                )
            ).scalars().one()
            ticket_profile.is_active = True
            ticket_profile.printer_name = "Desk Printer"
            db.commit()

        workstation_response = client.post(
            "/printing/qz/resolve",
            json={"workstation_key": "ws-ticket", "document_type": "TICKET"},
        )
        assert workstation_response.status_code == 200
        assert workstation_response.json()["printer"]["name"] == "Desk Printer"
        assert workstation_response.json()["printer"]["source"] == "workstation"

        with _tenant_db(SessionLocal, tenant_id) as db:
            ticket_profile = db.execute(
                select(WorkstationPrinterProfile).where(
                    WorkstationPrinterProfile.workstation_key == "ws-ticket",
                    WorkstationPrinterProfile.document_type == "TICKET",
                )
            ).scalars().one()
            ticket_profile.printer_name = None
            db.commit()

        default_override_response = client.post(
            "/printing/qz/resolve",
            json={"workstation_key": "ws-ticket", "document_type": "TICKET"},
        )
        assert default_override_response.status_code == 200
        assert default_override_response.json()["printer"]["name"] == ""
        assert (
            default_override_response.json()["printer"]["source"]
            == "workstation_default"
        )

        with _tenant_db(SessionLocal, tenant_id) as db:
            ticket_profile = db.execute(
                select(WorkstationPrinterProfile).where(
                    WorkstationPrinterProfile.workstation_key == "ws-ticket",
                    WorkstationPrinterProfile.document_type == "TICKET",
                )
            ).scalars().one()
            ticket_profile.is_active = False
            db.commit()

        destination_response = client.post(
            "/printing/qz/resolve",
            json={"workstation_key": "ws-ticket", "document_type": "TICKET"},
        )
        assert destination_response.status_code == 200
        assert (
            destination_response.json()["printer"]["name"]
            == "Destination Ticket Printer"
        )
        assert destination_response.json()["printer"]["source"] == "destination"

        with _tenant_db(SessionLocal, tenant_id) as db:
            destination = db.execute(
                select(PrintDestination).where(
                    PrintDestination.document_type == "TICKET",
                    PrintDestination.is_default.is_(True),
                )
            ).scalars().one()
            destination.delivery_config = {"qz_direct_print_enabled": True}
            db.commit()

        fallback_response = client.post(
            "/printing/qz/resolve",
            json={"workstation_key": "ws-ticket", "document_type": "TICKET"},
        )

    assert fallback_response.status_code == 200
    assert fallback_response.json()["printer"]["name"] == ""
    assert fallback_response.json()["printer"]["source"] == "default"


def test_qz_workstation_settings_page_updates_only_target_workstation(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="qz-workstation-admin.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_user(
        SessionLocal,
        email="tenant-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(
            client,
            email="tenant-admin@example.com",
            password="TestPass123!",
            next_path="/admin/printing/workstations",
        )
        client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "ws-1"},
        )
        client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "ws-2"},
        )

        page = client.get("/admin/printing/workstations")
        assert page.status_code == 200
        assert "Workstations" in page.text
        assert "ws-1" in page.text
        assert "ws-2" in page.text
        assert "QZ_PRIVATE_KEY_TEXT" not in page.text
        assert "/qz/sign" not in page.text

        csrf = _prime_csrf(client, path="/admin/printing/workstations")
        save = client.post(
            "/admin/printing/workstations/ws-1/update",
            data={
                "workstation_label": "Front Desk PC",
                "ticket_is_active": "1",
                "ticket_printer_name": "Front Desk Ticket Printer",
                "wtn_printer_name": "",
                "invoice_printer_name": "Accounts Printer",
                CSRF_FORM_FIELD: csrf,
            },
            follow_redirects=False,
        )

    assert save.status_code == 303
    assert save.headers["location"] == "/admin/printing/workstations?message=Workstation+settings+saved."

    with _tenant_db(SessionLocal, tenant_id) as db:
        rows = list(
            db.execute(
                select(WorkstationPrinterProfile).order_by(
                    WorkstationPrinterProfile.workstation_key.asc(),
                    WorkstationPrinterProfile.document_type.asc(),
                )
            ).scalars()
        )

    ws1_rows = [row for row in rows if row.workstation_key == "ws-1"]
    ws2_rows = [row for row in rows if row.workstation_key == "ws-2"]
    assert len(ws1_rows) == 3
    assert len(ws2_rows) == 3
    assert all(row.workstation_label == "Front Desk PC" for row in ws1_rows)
    ticket_row = next(row for row in ws1_rows if row.document_type == "TICKET")
    invoice_row = next(row for row in ws1_rows if row.document_type == "INVOICE")
    wtn_row = next(row for row in ws1_rows if row.document_type == "WTN")
    assert bool(ticket_row.is_active) is True
    assert ticket_row.printer_name == "Front Desk Ticket Printer"
    assert invoice_row.printer_name == "Accounts Printer"
    assert bool(invoice_row.is_active) is False
    assert wtn_row.printer_name in (None, "")
    assert all((row.workstation_label or "") == "" for row in ws2_rows)
    assert all(bool(row.is_active) is False for row in ws2_rows)


def test_qz_workstation_resolution_is_tenant_isolated_for_same_workstation_key(
    tmp_path,
    monkeypatch,
):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="qz-workstation-tenant-isolation.db",
        monkeypatch=monkeypatch,
    )
    tenant_one = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    tenant_two = _seed_tenant(SessionLocal, name="Bravo", subdomain="bravo")
    _seed_user(
        SessionLocal,
        email="acme-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_one,
    )
    _seed_user(
        SessionLocal,
        email="bravo-admin@example.com",
        password="TestPass123!",
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_two,
    )
    _create_destination(
        SessionLocal,
        tenant_id=tenant_one,
        document_type="INVOICE",
        printer_name="Acme Invoice Printer",
    )
    _create_destination(
        SessionLocal,
        tenant_id=tenant_two,
        document_type="INVOICE",
        printer_name="Bravo Invoice Printer",
    )

    with TestClient(app, base_url="https://acme.localhost") as acme_client:
        _login(
            acme_client,
            email="acme-admin@example.com",
            password="TestPass123!",
            next_path="/admin/printing/workstations",
        )
        acme_client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "shared-workstation"},
        )
        with _tenant_db(SessionLocal, tenant_one) as db:
            invoice_profile = db.execute(
                select(WorkstationPrinterProfile).where(
                    WorkstationPrinterProfile.workstation_key == "shared-workstation",
                    WorkstationPrinterProfile.document_type == "INVOICE",
                )
            ).scalars().one()
            invoice_profile.is_active = True
            invoice_profile.printer_name = "Acme Front Office"
            db.commit()
        acme_response = acme_client.post(
            "/printing/qz/resolve",
            json={
                "workstation_key": "shared-workstation",
                "document_type": "INVOICE",
            },
        )

    with TestClient(app, base_url="https://bravo.localhost") as bravo_client:
        _login(
            bravo_client,
            email="bravo-admin@example.com",
            password="TestPass123!",
            next_path="/admin/printing/workstations",
        )
        bravo_client.post(
            "/printing/qz/workstation/register",
            json={"workstation_key": "shared-workstation"},
        )
        bravo_response = bravo_client.post(
            "/printing/qz/resolve",
            json={
                "workstation_key": "shared-workstation",
                "document_type": "INVOICE",
            },
        )

    assert acme_response.status_code == 200
    assert bravo_response.status_code == 200
    assert acme_response.json()["printer"]["name"] == "Acme Front Office"
    assert bravo_response.json()["printer"]["name"] == "Bravo Invoice Printer"


def test_non_admin_cannot_access_qz_workstation_settings_page(tmp_path, monkeypatch):
    app, SessionLocal = _build_app_and_session(
        tmp_path,
        db_name="qz-workstation-permissions.db",
        monkeypatch=monkeypatch,
    )
    tenant_id = _seed_tenant(SessionLocal, name="Acme", subdomain="acme")
    _seed_user(
        SessionLocal,
        email="operator@example.com",
        password="TestPass123!",
        role=ROLE_OPERATOR,
        tenant_id=tenant_id,
    )

    with TestClient(app, base_url="https://acme.localhost") as client:
        _login(client, email="operator@example.com", password="TestPass123!", next_path="/")
        response = client.get("/admin/printing/workstations")

    assert response.status_code == 403
