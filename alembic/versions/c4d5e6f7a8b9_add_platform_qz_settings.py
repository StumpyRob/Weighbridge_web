"""add platform qz settings

Revision ID: c4d5e6f7a8b9
Revises: bc2d3e4f5a6b
Create Date: 2026-03-26 20:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "bc2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "qz_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(sa.Column("qz_last_validated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("qz_last_validation_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("qz_last_validation_summary", sa.Text(), nullable=True))
        batch_op.alter_column("qz_enabled", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.drop_column("qz_last_validation_summary")
        batch_op.drop_column("qz_last_validation_status")
        batch_op.drop_column("qz_last_validated_at")
        batch_op.drop_column("qz_enabled")
