"""product accounting ready core fields

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-02-16 11:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "p6q7r8s9t0u1"
down_revision = "o5p6q7r8s9t0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("product_groups") as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=120), nullable=True))
        batch_op.add_column(
            sa.Column("nominal_code_default", sa.String(length=50), nullable=True)
        )

    op.execute("UPDATE product_groups SET name = code WHERE name IS NULL OR trim(name) = ''")

    with op.batch_alter_table("product_groups") as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(length=120), nullable=False)
        batch_op.create_unique_constraint("uq_product_groups_name", ["name"])

    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("nominal_code", sa.String(length=50), nullable=True))

    op.create_table(
        "customer_product_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_customer_product_prices_customer_id",
        "customer_product_prices",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_customer_product_prices_product_id",
        "customer_product_prices",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "uq_customer_product_prices_customer_product_active",
        "customer_product_prices",
        ["customer_id", "product_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active = true"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO product_groups
                (code, name, description, nominal_code_default, is_active, created_at, updated_at)
            SELECT
                :code, :name, :description, :nominal_code_default, :is_active, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1
                FROM product_groups
                WHERE lower(name) = lower(:name) OR lower(code) = lower(:code)
            )
            """
        ).bindparams(
            code="AGGREGATES",
            name="Aggregates",
            description="Aggregate materials",
            nominal_code_default="4000",
            is_active=True,
        )
    )

    op.execute(
        """
        UPDATE products
        SET group_id = (
            SELECT id
            FROM product_groups
            WHERE lower(name) = lower('Aggregates')
            ORDER BY id
            LIMIT 1
        )
        WHERE group_id IS NULL
          AND upper(code) IN ('TOPSOIL', 'GWASTE')
        """
    )


def downgrade() -> None:
    op.drop_index(
        "uq_customer_product_prices_customer_product_active",
        table_name="customer_product_prices",
    )
    op.drop_index(
        "ix_customer_product_prices_product_id",
        table_name="customer_product_prices",
    )
    op.drop_index(
        "ix_customer_product_prices_customer_id",
        table_name="customer_product_prices",
    )
    op.drop_table("customer_product_prices")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("nominal_code")

    with op.batch_alter_table("product_groups") as batch_op:
        batch_op.drop_constraint("uq_product_groups_name", type_="unique")
        batch_op.drop_column("nominal_code_default")
        batch_op.drop_column("name")
