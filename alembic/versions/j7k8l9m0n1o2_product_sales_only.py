"""add product sales_only flag

Revision ID: j7k8l9m0n1o2
Revises: i6j7k8l9m0n1
Create Date: 2026-02-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "j7k8l9m0n1o2"
down_revision = "i6j7k8l9m0n1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "sales_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.execute("UPDATE products SET sales_only = 0 WHERE sales_only IS NULL")

    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("sales_only", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("sales_only")
