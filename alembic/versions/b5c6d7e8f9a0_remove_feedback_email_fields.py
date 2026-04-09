"""remove feedback email fields

Revision ID: b5c6d7e8f9a0
Revises: a6c4e9b1d2f3
Create Date: 2026-04-09 22:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "a6c4e9b1d2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_feedback", schema=None) as batch_op:
        batch_op.drop_index("ix_user_feedback_tenant_email_status")
        batch_op.drop_column("host_name")
        batch_op.drop_column("recipient_email")
        batch_op.drop_column("email_delivery_status")
        batch_op.drop_column("email_delivery_error")


def downgrade() -> None:
    with op.batch_alter_table("user_feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("host_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("recipient_email", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("email_delivery_error", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "email_delivery_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.create_index(
            "ix_user_feedback_tenant_email_status",
            ["tenant_id", "email_delivery_status"],
            unique=False,
        )
