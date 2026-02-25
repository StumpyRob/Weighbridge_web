"""add vehicles default driver id

Revision ID: add_vehicles_default_driver_id
Revises: 1a2b3c4d5e6f
Create Date: 2026-02-25 16:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "add_vehicles_default_driver_id"
down_revision = "1a2b3c4d5e6f"
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
        columns = fk.get("constrained_columns") or []
        if constrained_column in columns:
            return True
    return False


def _index_exists(bind, table_name: str, index_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    for idx in inspector.get_indexes(table_name):
        if idx.get("name") == index_name:
            return True
        columns = idx.get("column_names") or []
        if columns == [column_name]:
            return True
    return False


def _ensure_vehicle_default_fk_column(
    *,
    bind,
    column_name: str,
    ref_table: str,
    fk_name: str,
    index_name: str,
) -> None:
    columns = _column_names(bind, "vehicles")
    if column_name not in columns:
        op.add_column("vehicles", sa.Column(column_name, sa.Integer(), nullable=True))
        columns.add(column_name)

    inspector = sa.inspect(bind)
    if inspector.has_table(ref_table) and not _fk_exists(bind, "vehicles", fk_name, column_name):
        op.create_foreign_key(
            fk_name,
            "vehicles",
            ref_table,
            [column_name],
            ["id"],
        )

    if not _index_exists(bind, "vehicles", index_name, column_name):
        op.create_index(index_name, "vehicles", [column_name], unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("vehicles"):
        return

    _ensure_vehicle_default_fk_column(
        bind=bind,
        column_name="default_customer_id",
        ref_table="customers",
        fk_name="fk_vehicles_default_customer_id",
        index_name="ix_vehicles_default_customer_id",
    )
    _ensure_vehicle_default_fk_column(
        bind=bind,
        column_name="default_haulier_id",
        ref_table="hauliers",
        fk_name="fk_vehicles_default_haulier_id",
        index_name="ix_vehicles_default_haulier_id",
    )
    _ensure_vehicle_default_fk_column(
        bind=bind,
        column_name="default_driver_id",
        ref_table="drivers",
        fk_name="fk_vehicles_default_driver_id",
        index_name="ix_vehicles_default_driver_id",
    )


def downgrade() -> None:
    # Drift-repair migration: no destructive downgrade.
    pass
