"""add initialization flag to company settings

Revision ID: b1c2d3e4f5a6
Revises: 666eb0498833
Create Date: 2026-03-03 12:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "666eb0498833"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("company_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_initialized",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("company_settings", schema=None) as batch_op:
        batch_op.drop_column("is_initialized")
