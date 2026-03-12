"""add platform ai settings

Revision ID: 5c7d8e9f0a1b
Revises: 1b2c3d4e5f6a
Create Date: 2026-03-12 22:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "5c7d8e9f0a1b"
down_revision = "1b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("default_ai_model", sa.String(length=32), nullable=True),
        sa.Column("ai_temperature", sa.Float(), nullable=True),
        sa.Column("ai_max_output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "ai_dashboard_insights_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("ai_dashboard_cache_ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("ai_default_response_style", sa.String(length=16), nullable=True),
        sa.Column("ai_default_focus", sa.String(length=16), nullable=True),
        sa.Column("ai_extra_global_instructions", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("platform_settings", schema=None) as batch_op:
        batch_op.alter_column("ai_dashboard_insights_enabled", server_default=None)


def downgrade() -> None:
    op.drop_table("platform_settings")
