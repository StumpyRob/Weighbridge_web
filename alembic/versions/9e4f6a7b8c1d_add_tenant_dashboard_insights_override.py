"""add tenant dashboard insights override

Revision ID: 9e4f6a7b8c1d
Revises: 5c7d8e9f0a1b
Create Date: 2026-03-13 10:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "9e4f6a7b8c1d"
down_revision = "5c7d8e9f0a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ai_dashboard_insights_override", sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("ai_dashboard_insights_override")
