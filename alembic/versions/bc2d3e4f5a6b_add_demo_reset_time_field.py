"""add demo reset time field to tenants

Revision ID: bc2d3e4f5a6b
Revises: ab1c2d3e4f5b
Create Date: 2026-03-24 17:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "bc2d3e4f5a6b"
down_revision = "ab1c2d3e4f5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("demo_reset_time_minutes", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("demo_reset_time_minutes")
