from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import settings


def _sqlite_unique_sets(db_path: Path, table_name: str) -> set[tuple[str, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        unique_sets: set[tuple[str, ...]] = set()
        for row in conn.execute(f"PRAGMA index_list('{table_name}')").fetchall():
            index_name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            columns = tuple(
                info_row[2]
                for info_row in conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
            )
            unique_sets.add(columns)
        return unique_sets
    finally:
        conn.close()


def _sqlite_columns(db_path: Path, table_name: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {str(row[1]) for row in rows}
    finally:
        conn.close()


def test_head_migration_removes_global_customer_and_vehicle_uniques(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))

    with tempfile.TemporaryDirectory(dir=root) as tmpdir:
        db_path = Path(tmpdir) / "tenant-uniqueness-migration.sqlite3"
        monkeypatch.setenv("database_url", f"sqlite+pysqlite:///{db_path.as_posix()}")
        monkeypatch.setenv("secret_key", "tenant-uniqueness-migration-test")
        monkeypatch.setattr(
            settings,
            "database_url",
            f"sqlite+pysqlite:///{db_path.as_posix()}",
        )

        command.upgrade(cfg, "head")

        tenant_columns = _sqlite_columns(db_path, "tenants")
        assert "is_demo" in tenant_columns

        customer_uniques = _sqlite_unique_sets(db_path, "customers")
        assert ("tenant_id", "account_code") in customer_uniques
        assert ("account_code",) not in customer_uniques

        vehicle_uniques = _sqlite_unique_sets(db_path, "vehicles")
        assert ("tenant_id", "registration") in vehicle_uniques
        assert ("registration",) not in vehicle_uniques
