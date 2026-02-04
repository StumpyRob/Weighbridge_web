"""ewc codes + product/ticket links

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-01-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    ewc_exists = "ewc_codes" in table_names
    if not ewc_exists:
        op.create_table(
            "ewc_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code_6", sa.String(length=6), nullable=False),
            sa.Column("code_display", sa.String(length=10), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column(
                "hazardous", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("source_file", sa.String(length=255), nullable=False),
            sa.Column("imported_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("code_6", name="uq_ewc_codes_code_6"),
        )
        ewc_exists = True
        ewc_index_names: set[str] = set()
    else:
        ewc_index_names = {
            idx.get("name") for idx in inspector.get_indexes("ewc_codes")
        }

    if ewc_exists and "idx_ewc_codes_code_6" not in ewc_index_names:
        op.create_index("idx_ewc_codes_code_6", "ewc_codes", ["code_6"])
    if ewc_exists and "idx_ewc_codes_active" not in ewc_index_names:
        op.create_index("idx_ewc_codes_active", "ewc_codes", ["active"])
    if ewc_exists and "idx_ewc_codes_hazardous" not in ewc_index_names:
        op.create_index("idx_ewc_codes_hazardous", "ewc_codes", ["hazardous"])

    if "products" in table_names:
        product_columns = {
            col["name"] for col in inspector.get_columns("products")
        }
        product_fk_exists = False
        for fk in inspector.get_foreign_keys("products"):
            if (
                fk.get("referred_table") == "ewc_codes"
                and fk.get("constrained_columns") == ["ewc_code_id"]
            ):
                product_fk_exists = True
                break

        needs_product_column = "ewc_code_id" not in product_columns
        needs_product_fk = not product_fk_exists
        if needs_product_column or needs_product_fk:
            with op.batch_alter_table("products") as batch_op:
                if needs_product_column:
                    batch_op.add_column(
                        sa.Column("ewc_code_id", sa.Integer(), nullable=True)
                    )
                if needs_product_fk:
                    batch_op.create_foreign_key(
                        "fk_products_ewc_code_id",
                        "ewc_codes",
                        ["ewc_code_id"],
                        ["id"],
                        ondelete="SET NULL",
                    )

    if "tickets" in table_names:
        ticket_columns = {col["name"] for col in inspector.get_columns("tickets")}
        ticket_additions: list[tuple[str, sa.Column]] = []
        if "ewc_code_6" not in ticket_columns:
            ticket_additions.append(
                ("ewc_code_6", sa.Column("ewc_code_6", sa.String(length=6)))
            )
        if "ewc_code_display" not in ticket_columns:
            ticket_additions.append(
                (
                    "ewc_code_display",
                    sa.Column("ewc_code_display", sa.String(length=10)),
                )
            )
        if "ewc_description" not in ticket_columns:
            ticket_additions.append(
                ("ewc_description", sa.Column("ewc_description", sa.Text()))
            )
        if "ewc_hazardous" not in ticket_columns:
            ticket_additions.append(
                ("ewc_hazardous", sa.Column("ewc_hazardous", sa.Boolean()))
            )
        if ticket_additions:
            with op.batch_alter_table("tickets") as batch_op:
                for _, column in ticket_additions:
                    batch_op.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "tickets" in table_names:
        ticket_columns = {col["name"] for col in inspector.get_columns("tickets")}
        drops = [
            "ewc_hazardous",
            "ewc_description",
            "ewc_code_display",
            "ewc_code_6",
        ]
        if any(name in ticket_columns for name in drops):
            with op.batch_alter_table("tickets") as batch_op:
                for name in drops:
                    if name in ticket_columns:
                        batch_op.drop_column(name)

    if "products" in table_names:
        product_columns = {
            col["name"] for col in inspector.get_columns("products")
        }
        if "ewc_code_id" in product_columns:
            with op.batch_alter_table("products") as batch_op:
                batch_op.drop_column("ewc_code_id")

    if "ewc_codes" in table_names:
        existing_indexes = {
            idx.get("name") for idx in inspector.get_indexes("ewc_codes")
        }
        for name in (
            "idx_ewc_codes_hazardous",
            "idx_ewc_codes_active",
            "idx_ewc_codes_code_6",
        ):
            if name in existing_indexes:
                op.drop_index(name, table_name="ewc_codes")
        op.drop_table("ewc_codes")
