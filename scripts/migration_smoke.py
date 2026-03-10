from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config


VOID_REASON_TYPE_SPLIT_REV = "l2m3n4o5p6q7"


def _sqlite_unique_indexes(db_path: Path, table_name: str) -> list[tuple[str, list[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
        unique_indexes: list[tuple[str, list[str]]] = []
        for row in rows:
            index_name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            index_cols_rows = conn.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
            columns = [col_row[2] for col_row in index_cols_rows]
            unique_indexes.append((index_name, columns))
        return unique_indexes
    finally:
        conn.close()


def _assert_table_unique_sets(
    db_path: Path,
    table_name: str,
    *,
    expected_unique: tuple[str, ...],
    forbidden_unique: tuple[str, ...],
) -> None:
    unique_indexes = _sqlite_unique_indexes(db_path, table_name)
    unique_sets = {tuple(cols) for _, cols in unique_indexes}
    assert expected_unique in unique_sets, (
        f"Expected unique{expected_unique} at head for {table_name}, got: "
        f"{unique_indexes}"
    )
    assert forbidden_unique not in unique_sets, (
        f"Unexpected legacy unique{forbidden_unique} at head for {table_name}, got: "
        f"{unique_indexes}"
    )


def _assert_head_unique(db_path: Path) -> None:
    unique_indexes = _sqlite_unique_indexes(db_path, "void_reasons")
    unique_sets = {tuple(cols) for _, cols in unique_indexes}
    assert ("code", "reason_type") in unique_sets, (
        "Expected unique(code, reason_type) at head, got: "
        f"{unique_indexes}"
    )
    assert ("code",) not in unique_sets, (
        "Unexpected unique(code) at head, got: "
        f"{unique_indexes}"
    )
    _assert_table_unique_sets(
        db_path,
        "customers",
        expected_unique=("tenant_id", "account_code"),
        forbidden_unique=("account_code",),
    )
    _assert_table_unique_sets(
        db_path,
        "vehicles",
        expected_unique=("tenant_id", "registration"),
        forbidden_unique=("registration",),
    )


def _assert_prev_unique(db_path: Path) -> None:
    unique_indexes = _sqlite_unique_indexes(db_path, "void_reasons")
    unique_sets = {tuple(cols) for _, cols in unique_indexes}
    assert ("code",) in unique_sets, (
        "Expected unique(code) after downgrading before composite unique, got: "
        f"{unique_indexes}"
    )
    assert ("code", "reason_type") not in unique_sets, (
        "Unexpected unique(code, reason_type) after downgrading before composite unique, got: "
        f"{unique_indexes}"
    )


def _insert_legacy_invoice_codes(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO void_reasons "
            "(code, reason_type, description, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("Invoice entered in error", "INVOICE", "Entered in error", 1),
        )
        conn.execute(
            "INSERT INTO void_reasons "
            "(code, reason_type, description, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("Invoice customer cancelled", "INVOICE", "Customer cancelled", 1),
        )
        conn.commit()
    finally:
        conn.close()


def _assert_no_legacy_invoice_codes(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT code FROM void_reasons "
            "WHERE upper(reason_type) = 'INVOICE' "
            "AND lower(trim(code)) IN ('invoice entered in error', 'invoice customer cancelled')"
        ).fetchall()
        assert not rows, f"Legacy invoice codes still present after upgrade: {rows}"
    finally:
        conn.close()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "migration_smoke.sqlite3"
        os.environ["database_url"] = f"sqlite+pysqlite:///{db_path.as_posix()}"
        os.environ.setdefault("secret_key", "migration-smoke-secret")

        print("STEP: alembic downgrade base")
        command.downgrade(cfg, "base")

        print("STEP: alembic upgrade head")
        command.upgrade(cfg, "head")
        _assert_head_unique(db_path)

        print(f"STEP: alembic downgrade {VOID_REASON_TYPE_SPLIT_REV}")
        command.downgrade(cfg, VOID_REASON_TYPE_SPLIT_REV)
        _assert_prev_unique(db_path)
        _insert_legacy_invoice_codes(db_path)

        print("STEP: alembic upgrade head")
        command.upgrade(cfg, "head")
        _assert_head_unique(db_path)
        _assert_no_legacy_invoice_codes(db_path)

    print("Migration smoke test passed.")


if __name__ == "__main__":
    main()
