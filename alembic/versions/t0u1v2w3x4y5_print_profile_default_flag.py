"""add default flag to print profiles

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-02-20 10:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "t0u1v2w3x4y5"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("print_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            "ix_print_profiles_is_default",
            ["is_default"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("print_profiles") as batch_op:
        batch_op.drop_index("ix_print_profiles_is_default")
        batch_op.drop_column("is_default")
