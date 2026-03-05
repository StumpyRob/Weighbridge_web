from datetime import datetime
from io import BytesIO, StringIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import ROLE_SUPERADMIN, hash_password, user_identity_kwargs
from app.db import get_db
from app.main import app
from app.models import EwcCode, EwcImportLog
from app.models import User
from app.security_hardening import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from app.services.ewc_import import import_ewc_codes


def _admin_url(path: str) -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"https://admin.localhost{normalized}"


def _login_platform_superadmin(client) -> str:
    warm = client.get(_admin_url("/login"))
    assert warm.status_code == 200
    csrf = str(warm.cookies.get(CSRF_COOKIE_NAME) or client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    response = client.post(
        _admin_url("/login"),
        data={
            "email": "test-superadmin@example.com",
            "password": "TestPass123!",
            "next": "/admin/ewc-codes",
            CSRF_FORM_FIELD: csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    return csrf


@pytest.fixture()
def admin_client(SessionLocal):
    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with SessionLocal() as db:
        db.add(
            User(
                **user_identity_kwargs(email="test-superadmin@example.com", role=ROLE_SUPERADMIN),
                password_hash=hash_password("TestPass123!"),
                is_active=True,
                tenant_id=None,
            )
        )
        db.commit()

    with TestClient(app, base_url="https://admin.localhost") as client:
        _login_platform_superadmin(client)
        yield client

    app.dependency_overrides.clear()


def test_import_ewc_codes_is_idempotent_and_updates_existing_rows(db_session):
    first_csv = StringIO(
        "code,description,hazardous\n"
        "17 09 04,Mixed construction waste,false\n"
        "06 01 01*,Sulphuric acid,true\n"
    )
    result_first = import_ewc_codes(
        first_csv,
        db=db_session,
        source_name="ewc_first.csv",
        imported_by="test",
    )

    assert result_first.fatal_error is None
    assert result_first.inserted == 2
    assert result_first.updated == 0
    assert result_first.unchanged == 0
    assert result_first.deactivated == 0

    second_csv = StringIO(
        "code,description,hazardous\n"
        "17 09 04,Mixed construction waste,false\n"
        "06 01 01*,Sulphuric acid,true\n"
    )
    result_second = import_ewc_codes(
        second_csv,
        db=db_session,
        source_name="ewc_second.csv",
        imported_by="test",
    )
    assert result_second.inserted == 0
    assert result_second.updated == 0
    assert result_second.unchanged == 2
    assert result_second.deactivated == 0

    changed_csv = StringIO(
        "code,description,hazardous\n"
        "17 09 04,Updated description,false\n"
        "06 01 01*,Sulphuric acid,true\n"
    )
    result_third = import_ewc_codes(
        changed_csv,
        db=db_session,
        source_name="ewc_changed.csv",
        imported_by="test",
    )
    assert result_third.inserted == 0
    assert result_third.updated == 1
    assert result_third.unchanged == 1

    rows = db_session.execute(select(EwcCode).order_by(EwcCode.code_6)).scalars().all()
    assert len(rows) == 2
    assert rows[0].description == "Sulphuric acid"
    assert rows[1].description == "Updated description"


def test_import_ewc_codes_replace_mode_deactivates_missing_codes(db_session):
    seed_csv = StringIO(
        "code,description,hazardous\n"
        "17 09 04,Mixed construction waste,false\n"
        "06 01 01*,Sulphuric acid,true\n"
    )
    import_ewc_codes(seed_csv, db=db_session, source_name="seed.csv", imported_by="test")

    replace_csv = StringIO(
        "code,description,hazardous\n"
        "17 09 04,Mixed construction waste,false\n"
    )
    result = import_ewc_codes(
        replace_csv,
        replace=True,
        db=db_session,
        source_name="replace.csv",
        imported_by="test",
    )

    assert result.deactivated == 1
    rows = {row.code_6: row for row in db_session.execute(select(EwcCode)).scalars().all()}
    assert rows["170904"].active is True
    assert rows["060101"].active is False


def test_admin_ewc_import_page_uploads_csv_and_records_log(admin_client, db_session):
    csrf = str(admin_client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    page = admin_client.get(_admin_url("/admin/ewc-codes"))
    assert page.status_code == 200
    assert "EWC Codes" in page.text

    response = admin_client.post(
        _admin_url("/admin/ewc-codes"),
        data={"replace_existing": "1", CSRF_FORM_FIELD: csrf},
        files={
            "csv_file": (
                "ewc_upload.csv",
                BytesIO(
                    b"code,description,hazardous\n17 09 04,Mixed construction waste,false\n"
                ),
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ewc-codes?imported=1"

    rows = db_session.execute(select(EwcCode)).scalars().all()
    assert len(rows) == 1
    assert rows[0].code_6 == "170904"

    last_log = (
        db_session.execute(
            select(EwcImportLog).order_by(EwcImportLog.imported_at.desc(), EwcImportLog.id.desc())
        )
        .scalars()
        .first()
    )
    assert last_log is not None
    assert last_log.source_file == "ewc_upload.csv"
    assert last_log.inserted_count == 1


def test_admin_ewc_import_rejects_non_csv_upload(admin_client):
    csrf = str(admin_client.cookies.get(CSRF_COOKIE_NAME) or "")
    assert csrf
    response = admin_client.post(
        _admin_url("/admin/ewc-codes"),
        data={CSRF_FORM_FIELD: csrf},
        files={
            "csv_file": (
                "bad.txt",
                BytesIO(b"not csv"),
                "text/plain",
            )
        },
    )
    assert response.status_code == 400
    assert "Only .csv files are supported." in response.text


def test_admin_ewc_download_current_csv_exports_db_rows(admin_client, db_session):
    db_session.add(
        EwcCode(
            code_6="170904",
            code_display="17 09 04",
            description="Mixed construction waste",
            hazardous=False,
            active=True,
            source_file="seed.csv",
            imported_at=datetime(2026, 2, 25, 0, 0, 0),
        )
    )
    db_session.commit()

    response = admin_client.get(_admin_url("/admin/ewc-codes/sample.csv"))
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    assert "code,description,hazardous,active" in response.text
    assert "17 09 04,Mixed construction waste,false,true" in response.text
