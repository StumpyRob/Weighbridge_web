"""add company settings table with logo fields

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-02-22 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("address_line1", sa.String(length=120), nullable=True),
        sa.Column("address_line2", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("postcode", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        sa.Column("company_logo_path", sa.String(length=500), nullable=True),
        sa.Column("company_logo_updated_at", sa.DateTime(), nullable=True),
        sa.Column("logo_url", sa.String(length=500), nullable=True),
        sa.Column("logo_file_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("company_settings")
