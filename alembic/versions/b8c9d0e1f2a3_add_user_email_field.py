"""add user email field

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-03-14 12:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(length=150), nullable=True))

    op.execute(
        sa.text(
            "UPDATE users "
            "SET email = lower(trim(username)) "
            "WHERE email IS NULL OR trim(email) = ''"
        )
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "email",
            existing_type=sa.String(length=150),
            nullable=False,
        )

    op.create_index("ix_users_email", "users", ["email"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("email")
