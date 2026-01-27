"""lookup name fields and ticket indexes

Revision ID: b3c7a8d2e4f1
Revises: 6a2c8d1f4e90
Create Date: 2026-01-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "b3c7a8d2e4f1"
down_revision = "6a2c8d1f4e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.alter_column(
            "code",
            new_column_name="name",
            existing_type=sa.String(length=50),
            type_=sa.String(length=120),
            existing_nullable=False,
        )
        batch_op.drop_column("description")
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
    op.create_index("uq_hauliers_name", "hauliers", ["name"], unique=True)
    op.create_index("ix_hauliers_name", "hauliers", ["name"], unique=False)
    op.create_index(
        "ix_hauliers_is_active", "hauliers", ["is_active"], unique=False
    )

    with op.batch_alter_table("drivers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=150),
            type_=sa.String(length=120),
            existing_nullable=False,
        )
        batch_op.drop_column("description")
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
    op.create_index("uq_drivers_name", "drivers", ["name"], unique=True)
    op.create_index("ix_drivers_name", "drivers", ["name"], unique=False)
    op.create_index(
        "ix_drivers_is_active", "drivers", ["is_active"], unique=False
    )

    with op.batch_alter_table("containers") as batch_op:
        batch_op.alter_column(
            "code",
            new_column_name="name",
            existing_type=sa.String(length=50),
            type_=sa.String(length=120),
            existing_nullable=False,
        )
        batch_op.drop_column("description")
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
    op.create_index("uq_containers_name", "containers", ["name"], unique=True)
    op.create_index("ix_containers_name", "containers", ["name"], unique=False)
    op.create_index(
        "ix_containers_is_active", "containers", ["is_active"], unique=False
    )

    with op.batch_alter_table("destinations") as batch_op:
        batch_op.alter_column(
            "code",
            new_column_name="name",
            existing_type=sa.String(length=50),
            type_=sa.String(length=120),
            existing_nullable=False,
        )
        batch_op.drop_column("description")
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=sa.func.now(),
            existing_nullable=True,
        )
    op.create_index("uq_destinations_name", "destinations", ["name"], unique=True)
    op.create_index("ix_destinations_name", "destinations", ["name"], unique=False)
    op.create_index(
        "ix_destinations_is_active",
        "destinations",
        ["is_active"],
        unique=False,
    )

    op.create_index(
        "ix_tickets_haulier_id", "tickets", ["haulier_id"], unique=False
    )
    op.create_index("ix_tickets_driver_id", "tickets", ["driver_id"], unique=False)
    op.create_index(
        "ix_tickets_container_id", "tickets", ["container_id"], unique=False
    )
    op.create_index(
        "ix_tickets_destination_id", "tickets", ["destination_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_destination_id", table_name="tickets")
    op.drop_index("ix_tickets_container_id", table_name="tickets")
    op.drop_index("ix_tickets_driver_id", table_name="tickets")
    op.drop_index("ix_tickets_haulier_id", table_name="tickets")

    op.drop_index("ix_destinations_is_active", table_name="destinations")
    op.drop_index("ix_destinations_name", table_name="destinations")
    op.drop_index("uq_destinations_name", table_name="destinations")
    with op.batch_alter_table("destinations") as batch_op:
        batch_op.alter_column(
            "name",
            new_column_name="code",
            existing_type=sa.String(length=120),
            type_=sa.String(length=50),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("description", sa.String(length=255)))
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )

    op.drop_index("ix_containers_is_active", table_name="containers")
    op.drop_index("ix_containers_name", table_name="containers")
    op.drop_index("uq_containers_name", table_name="containers")
    with op.batch_alter_table("containers") as batch_op:
        batch_op.alter_column(
            "name",
            new_column_name="code",
            existing_type=sa.String(length=120),
            type_=sa.String(length=50),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("description", sa.String(length=255)))
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )

    op.drop_index("ix_drivers_is_active", table_name="drivers")
    op.drop_index("ix_drivers_name", table_name="drivers")
    op.drop_index("uq_drivers_name", table_name="drivers")
    with op.batch_alter_table("drivers") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=120),
            type_=sa.String(length=150),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("description", sa.String(length=255)))
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )

    op.drop_index("ix_hauliers_is_active", table_name="hauliers")
    op.drop_index("ix_hauliers_name", table_name="hauliers")
    op.drop_index("uq_hauliers_name", table_name="hauliers")
    with op.batch_alter_table("hauliers") as batch_op:
        batch_op.alter_column(
            "name",
            new_column_name="code",
            existing_type=sa.String(length=120),
            type_=sa.String(length=50),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("description", sa.String(length=255)))
        batch_op.alter_column(
            "is_active",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(),
            server_default=None,
            existing_nullable=True,
        )
