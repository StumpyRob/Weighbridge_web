"""lookup tables

Revision ID: 7a1c4f2d8e02
Revises: 3f2a9b7c4d11
Create Date: 2026-01-21 17:10:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "7a1c4f2d8e02"
down_revision = "3f2a9b7c4d11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hauliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "drivers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "containers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "destinations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "yards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "areas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "waste_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "haz_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "sic_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "licences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "waste_producers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "recyclers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "contractors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "tax_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "nominal_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "cost_centers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "invoice_frequencies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "void_reasons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "vehicle_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "product_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.add_column(sa.Column("vehicle_type_id", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("haulier_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("driver_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_vehicles_vehicle_type_id",
            "vehicle_types",
            ["vehicle_type_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_vehicles_haulier_id", "hauliers", ["haulier_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_vehicles_driver_id", "drivers", ["driver_id"], ["id"]
        )
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("nominal_code_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_products_group_id", "product_groups", ["group_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_products_unit_id", "units", ["unit_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_products_tax_rate_id", "tax_rates", ["tax_rate_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_products_nominal_code_id",
            "nominal_codes",
            ["nominal_code_id"],
            ["id"],
        )
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("haulier_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("driver_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("container_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("destination_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("yard_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("area_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("waste_code_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("waste_producer_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("licence_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tickets_haulier_id", "hauliers", ["haulier_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_driver_id", "drivers", ["driver_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_container_id", "containers", ["container_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_destination_id",
            "destinations",
            ["destination_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_tickets_yard_id", "yards", ["yard_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_area_id", "areas", ["area_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_waste_code_id", "waste_codes", ["waste_code_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_tickets_waste_producer_id",
            "waste_producers",
            ["waste_producer_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_tickets_licence_id", "licences", ["licence_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_constraint("fk_tickets_licence_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_waste_producer_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_waste_code_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_area_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_yard_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_destination_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_container_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_driver_id", type_="foreignkey")
        batch_op.drop_constraint("fk_tickets_haulier_id", type_="foreignkey")
        batch_op.drop_column("licence_id")
        batch_op.drop_column("waste_producer_id")
        batch_op.drop_column("waste_code_id")
        batch_op.drop_column("area_id")
        batch_op.drop_column("yard_id")
        batch_op.drop_column("destination_id")
        batch_op.drop_column("container_id")
        batch_op.drop_column("driver_id")
        batch_op.drop_column("haulier_id")
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint("fk_products_nominal_code_id", type_="foreignkey")
        batch_op.drop_constraint("fk_products_tax_rate_id", type_="foreignkey")
        batch_op.drop_constraint("fk_products_unit_id", type_="foreignkey")
        batch_op.drop_constraint("fk_products_group_id", type_="foreignkey")
        batch_op.drop_column("nominal_code_id")
        batch_op.drop_column("group_id")
    with op.batch_alter_table("vehicles") as batch_op:
        batch_op.drop_constraint("fk_vehicles_driver_id", type_="foreignkey")
        batch_op.drop_constraint("fk_vehicles_haulier_id", type_="foreignkey")
        batch_op.drop_constraint("fk_vehicles_vehicle_type_id", type_="foreignkey")
        batch_op.drop_column("driver_id")
        batch_op.drop_column("haulier_id")
        batch_op.drop_column("vehicle_type_id")
    op.drop_table("product_groups")
    op.drop_table("vehicle_types")
    op.drop_table("void_reasons")
    op.drop_table("invoice_frequencies")
    op.drop_table("cost_centers")
    op.drop_table("nominal_codes")
    op.drop_table("payment_methods")
    op.drop_table("tax_rates")
    op.drop_table("units")
    op.drop_table("contractors")
    op.drop_table("suppliers")
    op.drop_table("recyclers")
    op.drop_table("waste_producers")
    op.drop_table("licences")
    op.drop_table("sic_codes")
    op.drop_table("haz_codes")
    op.drop_table("waste_codes")
    op.drop_table("areas")
    op.drop_table("yards")
    op.drop_table("destinations")
    op.drop_table("containers")
    op.drop_table("drivers")
    op.drop_table("hauliers")
