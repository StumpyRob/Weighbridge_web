"""add company document email templates

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-03-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_settings",
        sa.Column("invoice_email_subject_template", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_settings",
        sa.Column("invoice_email_body_template", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_settings",
        sa.Column("ticket_email_subject_template", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_settings",
        sa.Column("ticket_email_body_template", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_settings", "ticket_email_body_template")
    op.drop_column("company_settings", "ticket_email_subject_template")
    op.drop_column("company_settings", "invoice_email_body_template")
    op.drop_column("company_settings", "invoice_email_subject_template")
