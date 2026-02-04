"""add ticket pricing snapshot fields

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-02-02 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "h4i5j6k7l8m9"
down_revision = "g3h4i5j6k7l8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("pricing_unit_name", sa.String(length=50), nullable=True))
    op.add_column("tickets", sa.Column("pricing_unit_type", sa.String(length=10), nullable=True))
    op.add_column("tickets", sa.Column("pricing_unit_price", sa.Numeric(12, 2), nullable=True))
    op.add_column("tickets", sa.Column("pricing_qty_snapshot", sa.Numeric(12, 3), nullable=True))
    op.add_column("tickets", sa.Column("pricing_net_kg_snapshot", sa.Numeric(12, 3), nullable=True))
    op.add_column("tickets", sa.Column("pricing_billable_qty_snapshot", sa.Numeric(12, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "pricing_billable_qty_snapshot")
    op.drop_column("tickets", "pricing_net_kg_snapshot")
    op.drop_column("tickets", "pricing_qty_snapshot")
    op.drop_column("tickets", "pricing_unit_price")
    op.drop_column("tickets", "pricing_unit_type")
    op.drop_column("tickets", "pricing_unit_name")
