"""force repair vehicles default lookup columns

Revision ID: veh_def_driver_fix
Revises: add_vehicles_default_driver_id
Create Date: 2026-02-25 18:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "veh_def_driver_fix"
down_revision = "add_vehicles_default_driver_id"
branch_labels = None
depends_on = None


def _column_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {str(col.get("name")) for col in inspector.get_columns(table_name)}


def _fk_exists(bind, table_name: str, fk_name: str, constrained_column: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("name") == fk_name:
            return True
        if constrained_column in (fk.get("constrained_columns") or []):
            return True
    return False


def _index_exists(bind, table_name: str, index_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    for idx in inspector.get_indexes(table_name):
        if idx.get("name") == index_name:
            return True
        if (idx.get("column_names") or []) == [column_name]:
            return True
    return False


def _ensure_column(bind, column_name: str) -> None:
    columns = _column_names(bind, "vehicles")
    if column_name in columns:
        return

    dialect = bind.dialect.name.lower()
    if dialect == "postgresql":
        op.execute(
            sa.text(
                f"ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS {column_name} INTEGER"
            )
        )
        return

    op.add_column("vehicles", sa.Column(column_name, sa.Integer(), nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("vehicles"):
        return

    for column in ("default_customer_id", "default_haulier_id", "default_driver_id"):
        _ensure_column(bind, column)

    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "drivers" in table_names and not _fk_exists(
        bind,
        "vehicles",
        "fk_vehicles_default_driver_id",
        "default_driver_id",
    ):
        op.create_foreign_key(
            "fk_vehicles_default_driver_id",
            "vehicles",
            "drivers",
            ["default_driver_id"],
            ["id"],
        )

    if "hauliers" in table_names and not _fk_exists(
        bind,
        "vehicles",
        "fk_vehicles_default_haulier_id",
        "default_haulier_id",
    ):
        op.create_foreign_key(
            "fk_vehicles_default_haulier_id",
            "vehicles",
            "hauliers",
            ["default_haulier_id"],
            ["id"],
        )

    if "customers" in table_names and not _fk_exists(
        bind,
        "vehicles",
        "fk_vehicles_default_customer_id",
        "default_customer_id",
    ):
        op.create_foreign_key(
            "fk_vehicles_default_customer_id",
            "vehicles",
            "customers",
            ["default_customer_id"],
            ["id"],
        )

    if not _index_exists(
        bind,
        "vehicles",
        "ix_vehicles_default_driver_id",
        "default_driver_id",
    ):
        op.create_index(
            "ix_vehicles_default_driver_id",
            "vehicles",
            ["default_driver_id"],
            unique=False,
        )


def downgrade() -> None:
    # Drift repair only; intentionally non-destructive.
    pass
