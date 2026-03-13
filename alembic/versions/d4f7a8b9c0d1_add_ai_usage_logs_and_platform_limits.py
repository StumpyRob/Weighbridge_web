"""add ai usage logs and platform limits

Revision ID: d4f7a8b9c0d1
Revises: 9e4f6a7b8c1d
Create Date: 2026-03-13 16:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d4f7a8b9c0d1"
down_revision = "9e4f6a7b8c1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assistant_requests_per_user_per_hour", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("assistant_requests_per_tenant_per_hour", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("dashboard_insights_min_refresh_seconds", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("dashboard_insights_max_per_tenant_per_hour", sa.Integer(), nullable=True))

    op.create_table(
        "ai_usage_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("request_type", sa.String(length=32), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("counted_toward_limit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_usage_logs_tenant_request_occurred",
        "ai_usage_logs",
        ["tenant_id", "request_type", "occurred_at"],
    )
    op.create_index(
        "ix_ai_usage_logs_user_request_occurred",
        "ai_usage_logs",
        ["user_id", "request_type", "occurred_at"],
    )
    op.create_index(
        "ix_ai_usage_logs_request_occurred",
        "ai_usage_logs",
        ["request_type", "occurred_at"],
    )
    op.create_index(
        "ix_ai_usage_logs_success_occurred",
        "ai_usage_logs",
        ["success", "occurred_at"],
    )

    with op.batch_alter_table("ai_usage_logs", schema=None) as batch_op:
        batch_op.alter_column("success", server_default=None)
        batch_op.alter_column("counted_toward_limit", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_ai_usage_logs_success_occurred", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_request_occurred", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_user_request_occurred", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_tenant_request_occurred", table_name="ai_usage_logs")
    op.drop_table("ai_usage_logs")

    with op.batch_alter_table("platform_settings", schema=None) as batch_op:
        batch_op.drop_column("dashboard_insights_max_per_tenant_per_hour")
        batch_op.drop_column("dashboard_insights_min_refresh_seconds")
        batch_op.drop_column("assistant_requests_per_tenant_per_hour")
        batch_op.drop_column("assistant_requests_per_user_per_hour")
