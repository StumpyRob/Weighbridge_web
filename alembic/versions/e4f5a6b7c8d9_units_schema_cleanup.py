"""units schema cleanup

Revision ID: e4f5a6b7c8d9
Revises: d2e3f4a5b6c7
Create Date: 2026-01-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "e4f5a6b7c8d9"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def _columns(conn) -> set[str]:
    rows = conn.execute(sa.text("PRAGMA table_info(units)")).fetchall()
    return {row[1] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn)

    if "name" not in cols and "code" in cols:
        with op.batch_alter_table("units") as batch_op:
            batch_op.alter_column(
                "code",
                new_column_name="name",
                existing_type=sa.String(length=50),
                type_=sa.String(length=50),
                existing_nullable=False,
            )
        cols = _columns(conn)

    if "description" in cols or "code" in cols:
        with op.batch_alter_table("units") as batch_op:
            if "description" in cols:
                batch_op.drop_column("description")
            if "code" in cols:
                batch_op.drop_column("code")

    cols = _columns(conn)
    if "is_active" not in cols:
        with op.batch_alter_table("units") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "is_active",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )
    if "created_at" not in cols:
        with op.batch_alter_table("units") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "created_at",
                    sa.DateTime(),
                    nullable=False,
                    server_default=sa.func.now(),
                )
            )
    if "updated_at" not in cols:
        with op.batch_alter_table("units") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "updated_at",
                    sa.DateTime(),
                    nullable=False,
                    server_default=sa.func.now(),
                )
            )

    conn.execute(
        sa.text(
            "UPDATE units SET "
            "created_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
            "updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"
        )
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_units_name ON units (name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_units_name ON units (name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_units_is_active ON units (is_active)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_units_is_active")
    op.execute("DROP INDEX IF EXISTS ix_units_name")
    op.execute("DROP INDEX IF EXISTS uq_units_name")
