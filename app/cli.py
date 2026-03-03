from __future__ import annotations

import argparse
from dataclasses import dataclass
from getpass import getpass
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .audit import diff as audit_diff
from .audit import log as audit_log
from .audit import user_snapshot
from .auth import ROLE_SUPERADMIN, hash_password, normalize_email, user_identity_kwargs, validate_email
from .db import SessionLocal
from .models import User


@dataclass(frozen=True)
class SuperadminCreationResult:
    id: int
    email: str
    role: str


def create_superadmin_account(
    *,
    email: str,
    password: str,
    session_factory: sessionmaker | Callable[[], Session] = SessionLocal,
) -> SuperadminCreationResult:
    normalized = normalize_email(email)
    if not validate_email(normalized):
        raise RuntimeError("A valid email address is required.")
    if len(str(password or "")) < 8:
        raise RuntimeError("Password must be at least 8 characters.")

    with session_factory() as db:
        user_count = int(db.execute(select(func.count(User.id))).scalar_one_or_none() or 0)
        if user_count > 0:
            raise RuntimeError("Superadmin bootstrap is only allowed when no users exist.")

        user = User(
            **user_identity_kwargs(email=normalized, role=ROLE_SUPERADMIN),
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.flush()
        snapshot_after = user_snapshot(user)
        audit_log(
            db,
            None,
            action="USER_CREATE",
            entity_type="user",
            entity_id=user.id,
            summary=f"Created user {snapshot_after.get('email') or snapshot_after.get('username') or user.id}",
            details=audit_diff(
                {},
                snapshot_after,
                ["username", "email", "role", "is_active"],
            ),
        )
        db.commit()
        db.refresh(user)
        return SuperadminCreationResult(
            id=int(user.id),
            email=normalized,
            role=ROLE_SUPERADMIN,
        )


def _prompt_missing(value: str | None, *, prompt: str, secret: bool = False) -> str:
    current = str(value or "").strip()
    if current:
        return current
    if secret:
        return getpass(prompt)
    return input(prompt).strip()


def _run_create_superadmin(args: argparse.Namespace) -> int:
    email = _prompt_missing(getattr(args, "email", None), prompt="Email: ")
    password = _prompt_missing(
        getattr(args, "password", None),
        prompt="Password (min 8 chars): ",
        secret=True,
    )
    result = create_superadmin_account(email=email, password=password)
    print(f"Created SUPERADMIN user {result.email} (id={result.id}).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create-superadmin",
        help="Create the first SUPERADMIN account (only when no users exist).",
    )
    create.add_argument("--email", default="", help="SUPERADMIN email address.")
    create.add_argument(
        "--password",
        default="",
        help="SUPERADMIN password (omit to be prompted).",
    )
    create.set_defaults(handler=_run_create_superadmin)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
