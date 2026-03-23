from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import settings


LEGACY_REVISION = "3c4d5e6f7a8b"
ROLES_REVISION = "7d8e9f0a1b2c"


def _sqlite_columns(db_path: Path, table_name: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {str(row[1]) for row in rows}
    finally:
        conn.close()


def test_wtn_signature_roles_migration_backfills_legacy_signature_into_receiver(
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))

    with tempfile.TemporaryDirectory(dir=root) as tmpdir:
        db_path = Path(tmpdir) / "wtn-signature-roles.sqlite3"
        database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
        monkeypatch.setenv("database_url", database_url)
        monkeypatch.setenv("secret_key", "wtn-signature-roles-migration-test")
        monkeypatch.setattr(settings, "database_url", database_url)

        command.upgrade(cfg, LEGACY_REVISION)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO tickets (
                    tenant_id,
                    ticket_no,
                    created_at,
                    updated_at,
                    datetime,
                    status,
                    direction,
                    transaction_type,
                    walk_in,
                    walk_in_sale,
                    ewc_manual_override,
                    dont_invoice,
                    paid,
                    wtn_signature_data_uri,
                    wtn_signature_signed_at,
                    wtn_signature_signer_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "T-WTN-MIG-1",
                    "2026-03-23 10:00:00",
                    "2026-03-23 10:00:00",
                    "2026-03-23 10:00:00",
                    "COMPLETE",
                    "INWARD",
                    "WASTEIN",
                    0,
                    0,
                    0,
                    0,
                    0,
                    "data:image/png;base64,legacy-signature",
                    "2026-03-23 10:05:00",
                    "Legacy Receiver",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        command.upgrade(cfg, ROLES_REVISION)

        columns = _sqlite_columns(db_path, "tickets")
        assert "wtn_producer_signature_data_uri" in columns
        assert "wtn_carrier_signature_data_uri" in columns
        assert "wtn_receiver_signature_data_uri" in columns

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT
                    wtn_signature_data_uri,
                    wtn_signature_signed_at,
                    wtn_signature_signer_name,
                    wtn_receiver_signature_data_uri,
                    wtn_receiver_signature_signed_at,
                    wtn_receiver_signature_signer_name
                FROM tickets
                WHERE ticket_no = ?
                """,
                ("T-WTN-MIG-1",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        (
            legacy_data_uri,
            legacy_signed_at,
            legacy_signer_name,
            receiver_data_uri,
            receiver_signed_at,
            receiver_signer_name,
        ) = row
        assert receiver_data_uri == legacy_data_uri
        assert receiver_signed_at == legacy_signed_at
        assert receiver_signer_name == legacy_signer_name


def test_wtn_signature_roles_migration_downgrade_removes_new_role_columns(
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))

    with tempfile.TemporaryDirectory(dir=root) as tmpdir:
        db_path = Path(tmpdir) / "wtn-signature-roles-downgrade.sqlite3"
        database_url = f"sqlite+pysqlite:///{db_path.as_posix()}"
        monkeypatch.setenv("database_url", database_url)
        monkeypatch.setenv("secret_key", "wtn-signature-roles-downgrade-test")
        monkeypatch.setattr(settings, "database_url", database_url)

        command.upgrade(cfg, ROLES_REVISION)
        columns_after_upgrade = _sqlite_columns(db_path, "tickets")
        assert "wtn_producer_signature_data_uri" in columns_after_upgrade
        assert "wtn_carrier_signature_data_uri" in columns_after_upgrade
        assert "wtn_receiver_signature_data_uri" in columns_after_upgrade

        command.downgrade(cfg, LEGACY_REVISION)
        columns_after_downgrade = _sqlite_columns(db_path, "tickets")
        assert "wtn_producer_signature_data_uri" not in columns_after_downgrade
        assert "wtn_carrier_signature_data_uri" not in columns_after_downgrade
        assert "wtn_receiver_signature_data_uri" not in columns_after_downgrade
