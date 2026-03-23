"""add wtn signature roles

Revision ID: 7d8e9f0a1b2c
Revises: 3c4d5e6f7a8b
Create Date: 2026-03-23 14:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "7d8e9f0a1b2c"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def _tickets_table() -> sa.Table:
    return sa.table(
        "tickets",
        sa.column("wtn_signature_data_uri", sa.Text()),
        sa.column("wtn_signature_signed_at", sa.DateTime()),
        sa.column("wtn_signature_signer_name", sa.String(length=120)),
        sa.column("wtn_receiver_signature_data_uri", sa.Text()),
        sa.column("wtn_receiver_signature_signed_at", sa.DateTime()),
        sa.column("wtn_receiver_signature_signer_name", sa.String(length=120)),
    )


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("wtn_producer_signature_data_uri", sa.Text(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_producer_signature_signed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_producer_signature_signer_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_carrier_signature_data_uri", sa.Text(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_carrier_signature_signed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_carrier_signature_signer_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_receiver_signature_data_uri", sa.Text(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_receiver_signature_signed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("wtn_receiver_signature_signer_name", sa.String(length=120), nullable=True),
    )

    tickets = _tickets_table()
    op.execute(
        tickets.update().where(
            sa.or_(
                tickets.c.wtn_receiver_signature_data_uri.is_(None),
                sa.func.trim(tickets.c.wtn_receiver_signature_data_uri) == "",
            )
        ).values(
            wtn_receiver_signature_data_uri=tickets.c.wtn_signature_data_uri,
            wtn_receiver_signature_signed_at=tickets.c.wtn_signature_signed_at,
            wtn_receiver_signature_signer_name=tickets.c.wtn_signature_signer_name,
        )
    )


def downgrade() -> None:
    op.drop_column("tickets", "wtn_receiver_signature_signer_name")
    op.drop_column("tickets", "wtn_receiver_signature_signed_at")
    op.drop_column("tickets", "wtn_receiver_signature_data_uri")
    op.drop_column("tickets", "wtn_carrier_signature_signer_name")
    op.drop_column("tickets", "wtn_carrier_signature_signed_at")
    op.drop_column("tickets", "wtn_carrier_signature_data_uri")
    op.drop_column("tickets", "wtn_producer_signature_signer_name")
    op.drop_column("tickets", "wtn_producer_signature_signed_at")
    op.drop_column("tickets", "wtn_producer_signature_data_uri")
