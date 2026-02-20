"""add print profiles lookup table

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-02-19 22:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "print_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("template_name", sa.String(length=255), nullable=False),
        sa.Column("transport_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "transport_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("code", name="uq_print_profiles_code"),
    )
    op.create_index(
        "ix_print_profiles_purpose",
        "print_profiles",
        ["purpose"],
        unique=False,
    )
    op.create_index(
        "ix_print_profiles_is_active",
        "print_profiles",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_print_profiles_is_active", table_name="print_profiles")
    op.drop_index("ix_print_profiles_purpose", table_name="print_profiles")
    op.drop_table("print_profiles")
