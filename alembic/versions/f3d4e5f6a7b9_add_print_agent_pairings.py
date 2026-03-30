"""add print agent pairing sessions

Revision ID: f3d4e5f6a7b9
Revises: f2c3d4e5f6a8
Create Date: 2026-03-29 17:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f3d4e5f6a7b9"
down_revision = "f2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "print_agent_pairings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("requested_name", sa.String(length=100), nullable=True),
        sa.Column("paired_name", sa.String(length=100), nullable=True),
        sa.Column("pairing_code_hash", sa.String(length=64), nullable=False),
        sa.Column("exchange_token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("paired_at", sa.DateTime(), nullable=True),
        sa.Column("paired_by_user_id", sa.Integer(), nullable=True),
        sa.Column("exchanged_at", sa.DateTime(), nullable=True),
        sa.Column("print_agent_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["paired_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["print_agent_id"], ["print_agents.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pairing_code_hash",
            name="uq_print_agent_pairings_pairing_code_hash",
        ),
        sa.UniqueConstraint(
            "exchange_token_hash",
            name="uq_print_agent_pairings_exchange_token_hash",
        ),
    )
    op.create_index(
        "ix_print_agent_pairings_tenant_id",
        "print_agent_pairings",
        ["tenant_id"],
    )
    op.create_index(
        "ix_print_agent_pairings_status",
        "print_agent_pairings",
        ["status"],
    )
    op.create_index(
        "ix_print_agent_pairings_expires_at",
        "print_agent_pairings",
        ["expires_at"],
    )
    op.create_index(
        "ix_print_agent_pairings_print_agent_id",
        "print_agent_pairings",
        ["print_agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_print_agent_pairings_print_agent_id",
        table_name="print_agent_pairings",
    )
    op.drop_index("ix_print_agent_pairings_expires_at", table_name="print_agent_pairings")
    op.drop_index("ix_print_agent_pairings_status", table_name="print_agent_pairings")
    op.drop_index("ix_print_agent_pairings_tenant_id", table_name="print_agent_pairings")
    op.drop_table("print_agent_pairings")
