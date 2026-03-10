"""add tenant demo marker

Revision ID: 4a8b9c0d1e2f
Revises: 3f1c2a4b5d6e
Create Date: 2026-03-10 17:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "4a8b9c0d1e2f"
down_revision = "3f1c2a4b5d6e"
branch_labels = None
depends_on = None

_TENANTS = sa.table(
    "tenants",
    sa.column("subdomain", sa.String()),
    sa.column("is_demo", sa.Boolean()),
)


def _demo_backfill_statement():
    normalized_subdomain = sa.func.lower(
        sa.func.trim(
            sa.func.coalesce(_TENANTS.c.subdomain, "")
        )
    )
    return (
        _TENANTS.update()
        .where(normalized_subdomain.in_(("demo", "default")))
        .values(is_demo=sa.true())
    )


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_demo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index("ix_tenants_is_demo", ["is_demo"], unique=False)

    op.execute(_demo_backfill_statement())

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.alter_column("is_demo", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_index("ix_tenants_is_demo")
        batch_op.drop_column("is_demo")
