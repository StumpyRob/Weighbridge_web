from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql


TENANT_UNIQUENESS_PREV_REV = "29d0e1f2a3b4"
TENANT_DEMO_FLAG_MIGRATION = "4a8b9c0d1e2f_add_tenant_demo_flag.py"


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


def _sqlite_table_columns(db_path: Path, table_name: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        return {str(row[1]) for row in rows}
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
    tenant_columns = _sqlite_table_columns(db_path, "tenants")
    assert "is_demo" in tenant_columns, (
        "Expected tenants.is_demo at head, got columns: "
        f"{sorted(tenant_columns)}"
    )
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


def _load_demo_flag_migration(root: Path):
    path = root / "alembic" / "versions" / TENANT_DEMO_FLAG_MIGRATION
    spec = importlib.util.spec_from_file_location("tenant_demo_flag_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_postgresql_demo_backfill_sql(root: Path) -> None:
    module = _load_demo_flag_migration(root)
    statement = module._demo_backfill_statement()
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "set is_demo=true" in compiled, compiled
    assert "set is_demo=1" not in compiled, compiled


def _assert_pre_repair_unique(db_path: Path) -> None:
    unique_indexes = _sqlite_unique_indexes(db_path, "void_reasons")
    unique_sets = {tuple(cols) for _, cols in unique_indexes}
    assert ("code", "reason_type") in unique_sets, (
        "Expected unique(code, reason_type) before the tenant-uniqueness repair, got: "
        f"{unique_indexes}"
    )
    customer_unique_sets = {
        tuple(cols) for _, cols in _sqlite_unique_indexes(db_path, "customers")
    }
    assert ("account_code",) in customer_unique_sets, (
        "Expected legacy global unique(account_code) before the repair, got: "
        f"{sorted(customer_unique_sets)}"
    )
    vehicle_unique_sets = {
        tuple(cols) for _, cols in _sqlite_unique_indexes(db_path, "vehicles")
    }
    assert ("registration",) in vehicle_unique_sets, (
        "Expected legacy global unique(registration) before the repair, got: "
        f"{sorted(vehicle_unique_sets)}"
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    _assert_postgresql_demo_backfill_sql(root)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "migration_smoke.sqlite3"
        os.environ["database_url"] = f"sqlite+pysqlite:///{db_path.as_posix()}"
        os.environ.setdefault("secret_key", "migration-smoke-secret")

        print("STEP: alembic downgrade base")
        command.downgrade(cfg, "base")

        print("STEP: alembic upgrade head")
        command.upgrade(cfg, "head")
        _assert_head_unique(db_path)

        print(f"STEP: alembic downgrade {TENANT_UNIQUENESS_PREV_REV}")
        command.downgrade(cfg, TENANT_UNIQUENESS_PREV_REV)
        _assert_pre_repair_unique(db_path)

        print("STEP: alembic upgrade head")
        command.upgrade(cfg, "head")
        _assert_head_unique(db_path)

    print("Migration smoke test passed.")


if __name__ == "__main__":
    main()
