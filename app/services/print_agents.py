from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PrintAgent, PrintAgentPairing
from ..models.base import utcnow

PRINT_AGENT_STATUS_ONLINE = "ONLINE"
PRINT_AGENT_STATUS_OFFLINE = "OFFLINE"
PRINT_AGENT_PAIRING_STATUS_PENDING = "PENDING"
PRINT_AGENT_PAIRING_STATUS_PAIRED = "PAIRED"
PRINT_AGENT_PAIRING_STATUS_EXCHANGED = "EXCHANGED"

_PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PAIRING_CODE_LENGTH = 8
_PAIRING_EXPIRY_MINUTES = 15


class PrintAgentPairingError(ValueError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)


def hash_print_agent_key(api_key: str) -> str:
    normalized = str(api_key or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_pairing_code(code: str | None) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def hash_print_agent_pairing_code(pairing_code: str | None) -> str:
    normalized = _normalize_pairing_code(pairing_code)
    if not normalized:
        return ""
    return hash_print_agent_key(normalized)


def generate_print_agent_api_key() -> tuple[str, str]:
    api_key = secrets.token_urlsafe(32)
    return api_key, hash_print_agent_key(api_key)


def generate_print_agent_credentials() -> tuple[str, str, str]:
    agent_id = str(uuid.uuid4())
    api_key, hashed_api_key = generate_print_agent_api_key()
    return agent_id, api_key, hashed_api_key


def generate_print_agent_pairing_session(
    *,
    expiry_minutes: int = _PAIRING_EXPIRY_MINUTES,
) -> tuple[str, str, str, str, str, datetime]:
    pairing_id = str(uuid.uuid4())
    raw_code = "".join(
        secrets.choice(_PAIRING_CODE_ALPHABET) for _ in range(_PAIRING_CODE_LENGTH)
    )
    pairing_code = f"{raw_code[:4]}-{raw_code[4:]}"
    exchange_token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(minutes=max(1, int(expiry_minutes)))
    return (
        pairing_id,
        pairing_code,
        exchange_token,
        hash_print_agent_pairing_code(pairing_code),
        hash_print_agent_key(exchange_token),
        expires_at,
    )


def is_print_agent_pairing_expired(pairing: PrintAgentPairing) -> bool:
    expires_at = getattr(pairing, "expires_at", None)
    return bool(expires_at and expires_at <= utcnow())


def create_print_agent_pairing(
    db: Session,
    *,
    requested_name: str | None,
) -> tuple[PrintAgentPairing, str, str]:
    pairing_id, pairing_code, exchange_token, code_hash, token_hash, expires_at = (
        generate_print_agent_pairing_session()
    )
    pairing = PrintAgentPairing(
        id=pairing_id,
        requested_name=str(requested_name or "").strip() or None,
        paired_name=None,
        pairing_code_hash=code_hash,
        exchange_token_hash=token_hash,
        status=PRINT_AGENT_PAIRING_STATUS_PENDING,
        expires_at=expires_at,
        paired_at=None,
        paired_by_user_id=None,
        exchanged_at=None,
        print_agent_id=None,
    )
    db.add(pairing)
    db.flush()
    return pairing, pairing_code, exchange_token


def get_print_agent_pairing_by_code(
    db: Session,
    pairing_code: str | None,
) -> PrintAgentPairing | None:
    hashed_code = hash_print_agent_pairing_code(pairing_code)
    if not hashed_code:
        return None
    return db.execute(
        select(PrintAgentPairing)
        .where(PrintAgentPairing.pairing_code_hash == hashed_code)
        .limit(1)
    ).scalars().first()


def complete_print_agent_pairing(
    db: Session,
    *,
    pairing_code: str | None,
    paired_by_user_id: int | None,
    agent_name: str | None,
) -> tuple[PrintAgentPairing, PrintAgent]:
    pairing = get_print_agent_pairing_by_code(db, pairing_code)
    if pairing is None:
        raise PrintAgentPairingError(
            "Print agent pairing code was not found.",
            status_code=404,
        )
    if is_print_agent_pairing_expired(pairing):
        raise PrintAgentPairingError(
            "Print agent pairing code has expired.",
            status_code=410,
        )
    status = str(pairing.status or "").strip().upper()
    if status == PRINT_AGENT_PAIRING_STATUS_PAIRED:
        raise PrintAgentPairingError(
            "Print agent pairing code has already been used.",
            status_code=409,
        )
    if status == PRINT_AGENT_PAIRING_STATUS_EXCHANGED:
        raise PrintAgentPairingError(
            "Print agent pairing credentials have already been issued.",
            status_code=409,
        )

    resolved_name = str(agent_name or "").strip() or pairing.requested_name
    agent_id, _placeholder_api_key, placeholder_api_key_hash = (
        generate_print_agent_credentials()
    )
    agent = PrintAgent(
        id=agent_id,
        name=resolved_name,
        api_key=placeholder_api_key_hash,
        status=PRINT_AGENT_STATUS_OFFLINE,
        last_seen_at=None,
    )
    db.add(agent)
    pairing.paired_name = resolved_name
    pairing.paired_by_user_id = paired_by_user_id
    pairing.paired_at = utcnow()
    pairing.print_agent_id = agent.id
    pairing.status = PRINT_AGENT_PAIRING_STATUS_PAIRED
    return pairing, agent


def exchange_print_agent_pairing(
    db: Session,
    *,
    pairing_id: str | None,
    exchange_token: str | None,
) -> tuple[PrintAgentPairing, PrintAgent, str]:
    normalized_pairing_id = str(pairing_id or "").strip()
    pairing = db.get(PrintAgentPairing, normalized_pairing_id) if normalized_pairing_id else None
    if pairing is None:
        raise PrintAgentPairingError(
            "Print agent pairing session was not found.",
            status_code=404,
        )
    hashed_token = hash_print_agent_key(str(exchange_token or "").strip())
    if not hashed_token or str(pairing.exchange_token_hash or "").strip() != hashed_token:
        raise PrintAgentPairingError(
            "Print agent pairing session was not found.",
            status_code=404,
        )
    if is_print_agent_pairing_expired(pairing):
        raise PrintAgentPairingError(
            "Print agent pairing session has expired.",
            status_code=410,
        )
    status = str(pairing.status or "").strip().upper()
    if status == PRINT_AGENT_PAIRING_STATUS_PENDING:
        raise PrintAgentPairingError(
            "Print agent pairing is not complete yet.",
            status_code=409,
        )
    if status == PRINT_AGENT_PAIRING_STATUS_EXCHANGED:
        raise PrintAgentPairingError(
            "Print agent pairing credentials have already been exchanged.",
            status_code=409,
        )
    if not pairing.print_agent_id:
        raise PrintAgentPairingError(
            "Print agent pairing is not complete yet.",
            status_code=409,
        )
    agent = db.get(PrintAgent, pairing.print_agent_id)
    if agent is None:
        raise PrintAgentPairingError(
            "Print agent pairing is not complete yet.",
            status_code=409,
        )

    raw_api_key, hashed_api_key = generate_print_agent_api_key()
    agent.api_key = hashed_api_key
    pairing.status = PRINT_AGENT_PAIRING_STATUS_EXCHANGED
    pairing.exchanged_at = utcnow()
    return pairing, agent, raw_api_key


def authenticate_print_agent(db: Session, api_key: str | None) -> PrintAgent | None:
    hashed_key = hash_print_agent_key(str(api_key or "").strip())
    if not hashed_key:
        return None
    return db.execute(
        select(PrintAgent).where(PrintAgent.api_key == hashed_key).limit(1)
    ).scalars().first()


def mark_print_agent_online(agent: PrintAgent) -> None:
    agent.status = PRINT_AGENT_STATUS_ONLINE
    agent.last_seen_at = utcnow()
