"""add tenant ai settings

Revision ID: 1b2c3d4e5f6a
Revises: f6a7b8c9d0e1
Create Date: 2026-03-12 17:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "1b2c3d4e5f6a"
down_revision = "4a8b9c0d1e2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("ai_model", sa.String(length=64), nullable=True))

    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.alter_column("ai_enabled", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("ai_model")
        batch_op.drop_column("ai_enabled")
