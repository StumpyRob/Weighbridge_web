"""units name fields

Revision ID: c1d2e3f4a5b6
Revises: b3c7a8d2e4f1
Create Date: 2026-01-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "b3c7a8d2e4f1"
branch_labels = None
depends_on = None


def _columns(conn) -> set[str]:
    rows = conn.execute(sa.text("PRAGMA table_info(units)")).fetchall()
    return {row[1] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn)
    if "code" in cols and "description" in cols:
        op.execute("UPDATE units SET code = COALESCE(description, code)")
    op.execute(
        "UPDATE units SET "
        "created_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
        "updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"
    )
    if "code" in cols or "description" in cols:
        with op.batch_alter_table("units") as batch_op:
            if "code" in cols:
                batch_op.alter_column(
                    "code",
                    new_column_name="name",
                    existing_type=sa.String(length=50),
                    type_=sa.String(length=50),
                    existing_nullable=False,
                )
            if "description" in cols:
                batch_op.drop_column("description")
            batch_op.alter_column(
                "is_active",
                existing_type=sa.Boolean(),
                server_default=sa.true(),
                existing_nullable=False,
            )
            batch_op.alter_column(
                "created_at",
                existing_type=sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
                existing_nullable=True,
            )
            batch_op.alter_column(
                "updated_at",
                existing_type=sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
                existing_nullable=True,
            )
    op.create_index("uq_units_name", "units", ["name"], unique=True)
    op.create_index("ix_units_name", "units", ["name"], unique=False)
    op.create_index("ix_units_is_active", "units", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_units_is_active", table_name="units")
    op.drop_index("ix_units_name", table_name="units")
    op.drop_index("uq_units_name", table_name="units")
    with op.batch_alter_table("units") as batch_op:
        batch_op.alter_column(
            "name",
            new_column_name="code",
            existing_type=sa.String(length=50),
            type_=sa.String(length=50),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("description", sa.String(length=255)))
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
