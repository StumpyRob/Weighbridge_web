"""add product_type to products

Revision ID: 0f1a2b3c4d5e
Revises: f1a2b3c4d5e6
Create Date: 2026-03-19 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0f1a2b3c4d5e"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("product_type", sa.String(length=16), nullable=True),
    )

    products = sa.table(
        "products",
        sa.column("product_type", sa.String(length=16)),
        sa.column("sales_only", sa.Boolean()),
        sa.column("ewc_code_id", sa.Integer()),
        sa.column("is_hazardous", sa.Boolean()),
        sa.column("final_disposal_wip", sa.Boolean()),
        sa.column("used_on_site_wip", sa.Boolean()),
    )

    backfill_type = sa.case(
        (products.c.sales_only.is_(sa.true()), sa.literal("sale")),
        (
            sa.or_(
                products.c.ewc_code_id.is_not(None),
                products.c.is_hazardous.is_(sa.true()),
                products.c.final_disposal_wip.is_(sa.true()),
                products.c.used_on_site_wip.is_(sa.true()),
            ),
            sa.literal("waste"),
        ),
        else_=sa.literal("sale"),
    )

    op.execute(
        products.update()
        .where(
            sa.or_(
                products.c.product_type.is_(None),
                sa.func.trim(products.c.product_type) == "",
            )
        )
        .values(product_type=backfill_type)
    )


def downgrade() -> None:
    op.drop_column("products", "product_type")
