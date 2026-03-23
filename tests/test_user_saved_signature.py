from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.auth import hash_password
from app.models import (
    AuditEvent,
    DirectionEnum,
    Ticket,
    TicketStatusEnum,
    TransactionTypeEnum,
    User,
)

SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR4nGP4DwQMDAz/ARruBPywhCTXAAAAAElFTkSuQmCC"
)
SECOND_SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR4nGNgYGD4DwQMDAwAAV0BBxSeDs0AAAAASUVORK5CYII="
)
BLANK_SIGNATURE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAC0lEQVR4nGP4DwUAI+UH+Yo0eLMAAAAASUVORK5CYII="
)


def _current_user(db_session) -> User:
    return (
        db_session.execute(select(User).order_by(User.id.asc()).limit(1))
        .scalars()
        .one()
    )


def _complete_waste_ticket(db_session, ticket_no: str) -> Ticket:
    ticket = Ticket(
        ticket_no=ticket_no,
        datetime=datetime(2026, 3, 23, 20, 30, 0),
        status=TicketStatusEnum.COMPLETE.value,
        direction=DirectionEnum.INWARD.value,
        transaction_type=TransactionTypeEnum.WASTEIN.value,
        ewc_code_display="17 09 04",
        ewc_description="Saved signature test waste",
        net_kg=Decimal("1250.000"),
        dont_invoice=False,
        paid=False,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_user_can_save_their_own_signature(client, db_session):
    user = _current_user(db_session)

    response = client.post(
        "/account/signature",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "signer_name": "Receiver Self",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account/signature?saved=1"

    db_session.refresh(user)
    assert user.saved_signature_data_uri == SIGNATURE_DATA_URL
    assert user.saved_signature_signer_name == "Receiver Self"
    assert user.saved_signature_updated_at is not None


def test_user_saved_signature_rejects_blank_signature(client, db_session):
    user = _current_user(db_session)

    response = client.post(
        "/account/signature",
        data={
            "signature_data_url": BLANK_SIGNATURE_DATA_URL,
            "signer_name": "Receiver Self",
        },
    )

    assert response.status_code == 400
    assert "Signature cannot be blank." in response.text

    db_session.refresh(user)
    assert user.saved_signature_data_uri in (None, "")
    assert user.saved_signature_updated_at is None


def test_user_saved_signature_scope_stays_self_only(client, db_session):
    current_user = _current_user(db_session)
    other_user = User(
        username="other-signature@example.com",
        email="other-signature@example.com",
        password_hash=hash_password("OtherPass123!"),
        is_active=True,
    )
    db_session.add(other_user)
    db_session.commit()

    response = client.post(
        "/account/signature",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "signer_name": "Only Me",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303

    db_session.refresh(current_user)
    db_session.refresh(other_user)
    assert current_user.saved_signature_data_uri == SIGNATURE_DATA_URL
    assert other_user.saved_signature_data_uri in (None, "")
    assert other_user.saved_signature_signer_name is None


def test_user_can_apply_saved_signature_to_receiver_role(client, db_session):
    user = _current_user(db_session)
    user.saved_signature_data_uri = SIGNATURE_DATA_URL
    user.saved_signature_signer_name = "Saved Receiver"
    user.saved_signature_updated_at = datetime(2026, 3, 23, 20, 10, 0)
    db_session.commit()

    ticket = _complete_waste_ticket(db_session, "T-USER-SIGN-APPLY")

    response = client.post(
        f"/tickets/{ticket.id}/wtn/signature/receiver/apply-saved",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/tickets/{ticket.id}?wtn_receiver_signature_applied=1"
    )

    db_session.refresh(ticket)
    assert ticket.wtn_receiver_signature_data_uri == SIGNATURE_DATA_URL
    assert ticket.wtn_receiver_signature_signer_name == "Saved Receiver"
    assert ticket.wtn_receiver_signature_signed_at is not None


def test_applying_saved_signature_without_one_is_rejected_cleanly(client, db_session):
    user = _current_user(db_session)
    user.saved_signature_data_uri = None
    user.saved_signature_signer_name = None
    user.saved_signature_updated_at = None
    db_session.commit()

    ticket = _complete_waste_ticket(db_session, "T-USER-SIGN-NONE")

    response = client.post(f"/tickets/{ticket.id}/wtn/signature/receiver/apply-saved")

    assert response.status_code == 400
    assert "You do not have a saved signature yet." in response.text

    db_session.refresh(ticket)
    assert ticket.wtn_receiver_signature_data_uri in (None, "")
    assert ticket.wtn_receiver_signature_signed_at is None


def test_ticket_keeps_copied_snapshot_when_user_signature_changes_later(client, db_session):
    user = _current_user(db_session)
    user.saved_signature_data_uri = SIGNATURE_DATA_URL
    user.saved_signature_signer_name = "Initial Receiver"
    user.saved_signature_updated_at = datetime(2026, 3, 23, 20, 15, 0)
    db_session.commit()

    ticket = _complete_waste_ticket(db_session, "T-USER-SIGN-SNAPSHOT")

    apply_response = client.post(
        f"/tickets/{ticket.id}/wtn/signature/receiver/apply-saved",
        follow_redirects=False,
    )
    assert apply_response.status_code == 303

    db_session.refresh(ticket)
    original_ticket_signature = ticket.wtn_receiver_signature_data_uri
    original_ticket_signer_name = ticket.wtn_receiver_signature_signer_name

    user.saved_signature_data_uri = SECOND_SIGNATURE_DATA_URL
    user.saved_signature_signer_name = "Updated Receiver"
    user.saved_signature_updated_at = datetime(2026, 3, 23, 20, 16, 0)
    db_session.commit()

    db_session.refresh(ticket)
    assert ticket.wtn_receiver_signature_data_uri == original_ticket_signature
    assert ticket.wtn_receiver_signature_signer_name == original_ticket_signer_name
    assert ticket.wtn_receiver_signature_data_uri == SIGNATURE_DATA_URL
    assert ticket.wtn_receiver_signature_signer_name == "Initial Receiver"


def test_saved_signature_save_and_apply_create_audit_events(client, db_session):
    user = _current_user(db_session)

    save_response = client.post(
        "/account/signature",
        data={
            "signature_data_url": SIGNATURE_DATA_URL,
            "signer_name": "Audit Receiver",
        },
        follow_redirects=False,
    )
    assert save_response.status_code == 303

    save_event = (
        db_session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "USER_SIGNATURE_SAVED",
                AuditEvent.entity_type == "user",
                AuditEvent.entity_id == str(user.id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    save_details = save_event.details_json or {}
    assert save_details.get("signer_name") == "Audit Receiver"
    assert save_details.get("operation") == "save"
    assert save_details.get("updated_at")

    ticket = _complete_waste_ticket(db_session, "T-USER-SIGN-AUDIT")
    apply_response = client.post(
        f"/tickets/{ticket.id}/wtn/signature/receiver/apply-saved",
        follow_redirects=False,
    )
    assert apply_response.status_code == 303

    apply_event = (
        db_session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.action == "TICKET_WTN_RECEIVER_SIG_APPLY",
                AuditEvent.entity_type == "ticket",
                AuditEvent.entity_id == str(ticket.id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    apply_details = apply_event.details_json or {}
    assert apply_details.get("ticket_id") == ticket.id
    assert apply_details.get("ticket_no") == ticket.ticket_no
    assert apply_details.get("applied_by_user_id") == user.id
    assert apply_details.get("applied_by_user_name")
    assert apply_details.get("signer_name") == "Audit Receiver"
    assert apply_details.get("operation") == "apply_saved_signature"
