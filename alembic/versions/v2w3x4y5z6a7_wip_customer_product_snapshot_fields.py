"""add wip customer product fields and snapshots

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-02-22 15:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "v2w3x4y5z6a7"
down_revision = "u1v2w3x4y5z6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(sa.Column("credit_limit_pence", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_cash_account",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "final_disposal_wip",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "used_on_site_wip",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("wip_snapshot_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("customer_snapshot_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("invoice_lines") as batch_op:
        batch_op.add_column(sa.Column("product_snapshot_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice_lines") as batch_op:
        batch_op.drop_column("product_snapshot_json")

    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_column("customer_snapshot_json")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("wip_snapshot_json")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("used_on_site_wip")
        batch_op.drop_column("final_disposal_wip")

    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("is_cash_account")
        batch_op.drop_column("credit_limit_pence")
