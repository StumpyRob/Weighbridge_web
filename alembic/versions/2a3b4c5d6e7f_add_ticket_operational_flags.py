"""add ticket operational flags

Revision ID: 2a3b4c5d6e7f
Revises: 0f1a2b3c4d5e
Create Date: 2026-03-19 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "2a3b4c5d6e7f"
down_revision = "0f1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("final_disposal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tickets",
        sa.Column("used_on_site", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    tickets = sa.table(
        "tickets",
        sa.column("id", sa.Integer()),
        sa.column("product_id", sa.Integer()),
        sa.column("final_disposal", sa.Boolean()),
        sa.column("used_on_site", sa.Boolean()),
    )
    products = sa.table(
        "products",
        sa.column("id", sa.Integer()),
        sa.column("final_disposal", sa.Boolean()),
        sa.column("used_on_site", sa.Boolean()),
        sa.column("final_disposal_wip", sa.Boolean()),
        sa.column("used_on_site_wip", sa.Boolean()),
    )

    final_disposal_default = (
        sa.select(
            sa.case(
                (
                    sa.or_(
                        products.c.final_disposal.is_(sa.true()),
                        products.c.final_disposal_wip.is_(sa.true()),
                    ),
                    sa.true(),
                ),
                else_=sa.false(),
            )
        )
        .where(products.c.id == tickets.c.product_id)
        .scalar_subquery()
    )
    used_on_site_default = (
        sa.select(
            sa.case(
                (
                    sa.or_(
                        products.c.used_on_site.is_(sa.true()),
                        products.c.used_on_site_wip.is_(sa.true()),
                    ),
                    sa.true(),
                ),
                else_=sa.false(),
            )
        )
        .where(products.c.id == tickets.c.product_id)
        .scalar_subquery()
    )

    op.execute(
        tickets.update().values(
            final_disposal=sa.func.coalesce(final_disposal_default, sa.false()),
            used_on_site=sa.func.coalesce(used_on_site_default, sa.false()),
        )
    )


def downgrade() -> None:
    op.drop_column("tickets", "used_on_site")
    op.drop_column("tickets", "final_disposal")
