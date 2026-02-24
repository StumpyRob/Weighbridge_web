"""lock built-in print templates with is_system flag

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-02-25 10:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(col["name"]) for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "print_templates"):
        return

    columns = _column_names(inspector, "print_templates")
    if "is_system" not in columns:
        op.add_column(
            "print_templates",
            sa.Column(
                "is_system",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    bind.execute(sa.text("UPDATE print_templates SET is_system = 0 WHERE is_system IS NULL"))
    bind.execute(
        sa.text(
            """
            UPDATE print_templates
            SET is_system = 1,
                is_active = 1
            WHERE lower(code) IN ('ticket_default', 'invoice_default', 'wtn_default')
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "print_templates"):
        return
    columns = _column_names(inspector, "print_templates")
    if "is_system" in columns:
        with op.batch_alter_table("print_templates") as batch_op:
            batch_op.drop_column("is_system")
