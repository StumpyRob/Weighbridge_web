"""add workstation printer profiles

Revision ID: d2e3f4a5b6c7
Revises: c4d5e6f7a8b9
Create Date: 2026-03-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workstation_printer_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workstation_key", sa.String(length=64), nullable=False),
        sa.Column("workstation_label", sa.String(length=120), nullable=True),
        sa.Column("document_type", sa.String(length=16), nullable=False),
        sa.Column("printer_name", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workstation_key",
            "document_type",
            name="uq_workstation_printer_profiles_tenant_key_document",
        ),
    )
    op.create_index(
        "ix_workstation_printer_profiles_tenant_id",
        "workstation_printer_profiles",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_tenant_workstation_key",
        "workstation_printer_profiles",
        ["tenant_id", "workstation_key"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_document_type",
        "workstation_printer_profiles",
        ["document_type"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_is_active",
        "workstation_printer_profiles",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workstation_printer_profiles_is_active",
        table_name="workstation_printer_profiles",
    )
    op.drop_index(
        "ix_workstation_printer_profiles_document_type",
        table_name="workstation_printer_profiles",
    )
    op.drop_index(
        "ix_workstation_printer_profiles_tenant_workstation_key",
        table_name="workstation_printer_profiles",
    )
    op.drop_index(
        "ix_workstation_printer_profiles_tenant_id",
        table_name="workstation_printer_profiles",
    )
    op.drop_table("workstation_printer_profiles")
