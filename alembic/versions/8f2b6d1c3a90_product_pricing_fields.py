"""product pricing fields

Revision ID: 8f2b6d1c3a90
Revises: 1d7a5b9c2e10
Create Date: 2026-01-21 20:20:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "8f2b6d1c3a90"
down_revision = "1d7a5b9c2e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("account_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("cash_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("min_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("max_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("max_qty", sa.Numeric(12, 3), nullable=True))
        batch_op.add_column(sa.Column("excess_trigger", sa.Numeric(12, 3), nullable=True))
        batch_op.add_column(sa.Column("excess_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(
            sa.Column("is_hazardous", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("final_disposal", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("used_on_site", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("default_waste_code_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_products_default_waste_code_id",
            "waste_codes",
            ["default_waste_code_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint(
            "fk_products_default_waste_code_id", type_="foreignkey"
        )
        batch_op.drop_column("default_waste_code_id")
        batch_op.drop_column("used_on_site")
        batch_op.drop_column("final_disposal")
        batch_op.drop_column("is_hazardous")
        batch_op.drop_column("excess_price")
        batch_op.drop_column("excess_trigger")
        batch_op.drop_column("max_qty")
        batch_op.drop_column("max_price")
        batch_op.drop_column("min_price")
        batch_op.drop_column("cash_price")
        batch_op.drop_column("account_price")
