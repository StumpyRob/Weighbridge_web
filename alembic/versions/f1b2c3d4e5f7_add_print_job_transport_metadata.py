"""add print job transport metadata

Revision ID: f1b2c3d4e5f7
Revises: f0a1b2c3d4e5
Create Date: 2026-03-29 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1b2c3d4e5f7"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.add_column(sa.Column("payload_format", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("payload_mime_type", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("provider_job_ref", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("provider_response_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("print_jobs") as batch_op:
        batch_op.drop_column("provider_response_json")
        batch_op.drop_column("provider_job_ref")
        batch_op.drop_column("payload_mime_type")
        batch_op.drop_column("payload_format")
