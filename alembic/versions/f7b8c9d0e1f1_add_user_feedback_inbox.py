"""add user feedback inbox

Revision ID: f7b8c9d0e1f1
Revises: f5a6b7c8d9e0
Create Date: 2026-04-09 23:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f7b8c9d0e1f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("source_title", sa.String(length=255), nullable=True),
        sa.Column("submitted_by_display_name", sa.String(length=120), nullable=True),
        sa.Column("submitted_by_email", sa.String(length=255), nullable=True),
        sa.Column("host_name", sa.String(length=255), nullable=True),
        sa.Column("recipient_email", sa.String(length=255), nullable=True),
        sa.Column("email_delivery_status", sa.String(length=20), nullable=False),
        sa.Column("email_delivery_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_feedback_tenant_created",
        "user_feedback",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_feedback_tenant_status",
        "user_feedback",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_user_feedback_tenant_kind",
        "user_feedback",
        ["tenant_id", "kind"],
        unique=False,
    )
    op.create_index(
        "ix_user_feedback_tenant_email_status",
        "user_feedback",
        ["tenant_id", "email_delivery_status"],
        unique=False,
    )
    op.create_index(
        "ix_user_feedback_submitted_by",
        "user_feedback",
        ["submitted_by_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_feedback_submitted_by", table_name="user_feedback")
    op.drop_index("ix_user_feedback_tenant_email_status", table_name="user_feedback")
    op.drop_index("ix_user_feedback_tenant_kind", table_name="user_feedback")
    op.drop_index("ix_user_feedback_tenant_status", table_name="user_feedback")
    op.drop_index("ix_user_feedback_tenant_created", table_name="user_feedback")
    op.drop_table("user_feedback")
