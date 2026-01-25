"""ticket_no unique constraint

Revision ID: 6a2c8d1f4e90
Revises: 4e7b9a2c1d80
Create Date: 2026-01-23 09:50:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "6a2c8d1f4e90"
down_revision = "4e7b9a2c1d80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.create_unique_constraint("uq_tickets_ticket_no", ["ticket_no"])


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint("uq_tickets_ticket_no", type_="unique")
