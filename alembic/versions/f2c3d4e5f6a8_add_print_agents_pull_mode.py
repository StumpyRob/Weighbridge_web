"""add print agents and pull mode linkage

Revision ID: f2c3d4e5f6a8
Revises: f1b2c3d4e5f7
Create Date: 2026-03-29 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f2c3d4e5f6a8"
down_revision = "f1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "print_agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("api_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key", name="uq_print_agents_api_key"),
    )
    op.create_index("ix_print_agents_tenant_id", "print_agents", ["tenant_id"])
    op.create_index("ix_print_agents_status", "print_agents", ["status"])
    op.create_index("ix_print_agents_last_seen_at", "print_agents", ["last_seen_at"])

    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_print_jobs_agent_id_print_agents",
            "print_agents",
            ["agent_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_constraint("fk_print_jobs_agent_id_print_agents", type_="foreignkey")
        batch_op.drop_column("agent_id")

    op.drop_index("ix_print_agents_last_seen_at", table_name="print_agents")
    op.drop_index("ix_print_agents_status", table_name="print_agents")
    op.drop_index("ix_print_agents_tenant_id", table_name="print_agents")
    op.drop_table("print_agents")
