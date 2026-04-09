from sqlalchemy import select

from app.auth import verify_password
from app.models import AuditEvent, User


def _current_user(db_session) -> User:
    return (
        db_session.execute(select(User).order_by(User.id.asc()).limit(1))
        .scalars()
        .one()
    )


def test_account_page_renders_menu_and_forms(client):
    response = client.get("/account")

    assert response.status_code == 200
    assert "My Account" in response.text
    assert "Profile Details" in response.text
    assert "Password &amp; Security" in response.text
    assert "Open My Signature" in response.text
    assert "data-account-menu" in response.text


def test_account_profile_email_change_requires_current_password(client):
    response = client.post(
        "/account/profile",
        data={
            "first_name": "Test",
            "last_name": "User",
            "email": "new-superadmin@example.com",
            "current_password": "",
        },
    )

    assert response.status_code == 400
    assert "Current password is required to change your sign-in email." in response.text


def test_account_profile_update_saves_identity_and_audit(client, db_session):
    user = _current_user(db_session)

    response = client.post(
        "/account/profile",
        data={
            "first_name": "Taylor",
            "last_name": "Admin",
            "email": "updated-superadmin@example.com",
            "current_password": "TestPass123!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account?profile_saved=1"

    db_session.refresh(user)
    assert user.first_name == "Taylor"
    assert user.last_name == "Admin"
    assert user.email == "updated-superadmin@example.com"
    assert user.username == "updated-superadmin@example.com"

    event = (
        db_session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "USER_UPDATE",
                AuditEvent.entity_type == "user",
                AuditEvent.entity_id == str(user.id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    changed = (event.details_json or {}).get("changed", {})
    assert changed["email"]["to"] == "updated-superadmin@example.com"
    assert changed["first_name"]["to"] == "Taylor"
    assert changed["last_name"]["to"] == "Admin"


def test_account_password_change_updates_hash_and_audit(client, db_session):
    user = _current_user(db_session)

    response = client.post(
        "/account/password",
        data={
            "current_password": "TestPass123!",
            "password": "NewPass123!",
            "confirm_password": "NewPass123!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account?password_saved=1"

    db_session.refresh(user)
    assert verify_password("NewPass123!", user.password_hash)

    event = (
        db_session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "USER_PASSWORD_CHANGE",
                AuditEvent.entity_type == "user",
                AuditEvent.entity_id == str(user.id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    assert (event.details_json or {}).get("self_service") is True
    assert ((event.details_json or {}).get("changed") or {}).get("password", {}).get(
        "reset"
    ) is True
