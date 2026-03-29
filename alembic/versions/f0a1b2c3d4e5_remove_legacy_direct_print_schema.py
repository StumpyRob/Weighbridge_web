"""remove legacy direct print schema

Revision ID: f0a1b2c3d4e5
Revises: e8f9a0b1c2d3
Create Date: 2026-03-27 01:30:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f0a1b2c3d4e5"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def _drop_legacy_direct_print_columns_if_present() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "platform_settings" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"]
        for column in inspector.get_columns("platform_settings")
    }
    prefix = "".join(("q", "z", "_"))
    legacy_columns = tuple(
        column_name
        for column_name in existing_columns
        if column_name.startswith(prefix)
    )
    if not legacy_columns:
        return

    with op.batch_alter_table("platform_settings") as batch_op:
        for column_name in legacy_columns:
            batch_op.drop_column(column_name)


def _drop_legacy_workstation_profile_table_if_present() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "_".join(("workstation", "printer", "profiles"))
    if table_name not in inspector.get_table_names():
        return

    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes(table_name)
    }
    for index_name in (
        "_".join(("ix", "workstation", "printer", "profiles", "is", "active")),
        "_".join(("ix", "workstation", "printer", "profiles", "document", "type")),
        "_".join(("ix", "workstation", "printer", "profiles", "tenant", "workstation", "key")),
        "_".join(("ix", "workstation", "printer", "profiles", "tenant", "id")),
    ):
        if index_name in existing_indexes:
            op.drop_index(
                index_name,
                table_name=table_name,
            )
    op.drop_table(table_name)


def _remove_legacy_direct_print_keys_from_destinations() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "print_destinations" not in inspector.get_table_names():
        return

    print_destinations = sa.table(
        "print_destinations",
        sa.column("id", sa.Integer()),
        sa.column("delivery_config", sa.JSON()),
    )

    rows = bind.execute(
        sa.select(
            print_destinations.c.id,
            print_destinations.c.delivery_config,
        )
    ).fetchall()
    for row in rows:
        delivery_config = row.delivery_config
        if delivery_config is None:
            continue
        if isinstance(delivery_config, str):
            try:
                delivery_config = json.loads(delivery_config)
            except json.JSONDecodeError:
                continue
        if not isinstance(delivery_config, dict):
            continue

        cleaned = dict(delivery_config)
        changed = False
        prefix = "".join(("q", "z", "_"))
        for key in (
            f"{prefix}direct_print_enabled",
            f"{prefix}printer_name",
        ):
            if key in cleaned:
                cleaned.pop(key, None)
                changed = True
        if not changed:
            continue

        bind.execute(
            print_destinations.update()
            .where(print_destinations.c.id == int(row.id))
            .values(delivery_config=cleaned)
        )


def upgrade() -> None:
    _remove_legacy_direct_print_keys_from_destinations()
    _drop_legacy_workstation_profile_table_if_present()
    _drop_legacy_direct_print_columns_if_present()


def downgrade() -> None:
    prefix = "".join(("q", "z", "_"))
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(sa.Column(f"{prefix}enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column(f"{prefix}last_validated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}last_validation_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}last_validation_summary", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}certificate_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}private_key_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}certificate_updated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column(f"{prefix}private_key_updated_at", sa.DateTime(), nullable=True))
        batch_op.alter_column(f"{prefix}enabled", server_default=None)

    op.create_table(
        "workstation_printer_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("workstation_key", sa.String(length=64), nullable=False),
        sa.Column("workstation_label", sa.String(length=120), nullable=True),
        sa.Column("document_type", sa.String(length=16), nullable=False),
        sa.Column("printer_name", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workstation_key",
            "document_type",
            name="uq_workstation_printer_profiles_tenant_key_document",
        ),
    )
    op.create_index(
        "ix_workstation_printer_profiles_tenant_id",
        "workstation_printer_profiles",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_tenant_workstation_key",
        "workstation_printer_profiles",
        ["tenant_id", "workstation_key"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_document_type",
        "workstation_printer_profiles",
        ["document_type"],
        unique=False,
    )
    op.create_index(
        "ix_workstation_printer_profiles_is_active",
        "workstation_printer_profiles",
        ["is_active"],
        unique=False,
    )
