"""ticket ewc manual override flag

Revision ID: k1l2m3n4o5p6
Revises: j7k8l9m0n1o2
Create Date: 2026-02-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "k1l2m3n4o5p6"
down_revision = "j7k8l9m0n1o2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ticket_columns = {col["name"] for col in inspector.get_columns("tickets")}
    if "ewc_manual_override" not in ticket_columns:
        with op.batch_alter_table("tickets") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "ewc_manual_override",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    op.execute(
        "UPDATE tickets SET ewc_manual_override = 0 WHERE ewc_manual_override IS NULL"
    )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column("ewc_manual_override", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ticket_columns = {col["name"] for col in inspector.get_columns("tickets")}
    if "ewc_manual_override" in ticket_columns:
        with op.batch_alter_table("tickets") as batch_op:
            batch_op.drop_column("ewc_manual_override")
