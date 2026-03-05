"""user identity uniqueness tenant scope

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-05 16:30:00.000000
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {
        str(item.get("name") or "").strip()
        for item in inspector.get_indexes(table_name)
        if str(item.get("name") or "").strip()
    }


def _drop_indexes_if_present(table_name: str, names: Iterable[str]) -> None:
    existing = _index_names(table_name)
    for name in names:
        if name in existing:
            op.drop_index(name, table_name=table_name)


def _sqlite_rebuild_users(*, global_unique_username: bool) -> None:
    conn = op.get_bind()
    conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        op.create_table(
            "users__tmp",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=150), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name="fk_users_tenant_id_tenants",
            ),
            sa.PrimaryKeyConstraint("id"),
            *( [sa.UniqueConstraint("username")] if global_unique_username else [] ),
        )
        conn.execute(
            sa.text(
                "INSERT INTO users__tmp (id, username, tenant_id, role, password_hash, is_active, created_at) "
                "SELECT id, username, tenant_id, role, password_hash, is_active, created_at FROM users"
            )
        )
        op.drop_table("users")
        op.rename_table("users__tmp", "users")

        op.create_index("ix_users_tenant_id", "users", ["tenant_id"], unique=False)
        op.create_index("ix_users_role", "users", ["role"], unique=False)
        if not global_unique_username:
            op.create_index(
                "uq_users_tenant_username",
                "users",
                ["tenant_id", "username"],
                unique=True,
                sqlite_where=sa.text("tenant_id IS NOT NULL"),
            )
            op.create_index(
                "uq_users_platform_username",
                "users",
                ["username"],
                unique=True,
                sqlite_where=sa.text("tenant_id IS NULL"),
            )
    finally:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))


def _upgrade_non_sqlite() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for constraint in inspector.get_unique_constraints("users"):
        columns = list(constraint.get("column_names") or [])
        name = str(constraint.get("name") or "").strip()
        if columns == ["username"] and name:
            op.drop_constraint(name, "users", type_="unique")

    for index in inspector.get_indexes("users"):
        columns = list(index.get("column_names") or [])
        name = str(index.get("name") or "").strip()
        is_unique = bool(index.get("unique"))
        if columns == ["username"] and is_unique and name:
            op.drop_index(name, table_name="users")

    op.create_index(
        "uq_users_tenant_username",
        "users",
        ["tenant_id", "username"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "uq_users_platform_username",
        "users",
        ["username"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )


def _downgrade_non_sqlite() -> None:
    _drop_indexes_if_present(
        "users",
        ("uq_users_tenant_username", "uq_users_platform_username"),
    )
    op.create_unique_constraint("uq_users_username", "users", ["username"])


def upgrade() -> None:
    dialect = str(op.get_bind().dialect.name or "").lower()
    if dialect == "sqlite":
        _sqlite_rebuild_users(global_unique_username=False)
        return
    _upgrade_non_sqlite()


def downgrade() -> None:
    dialect = str(op.get_bind().dialect.name or "").lower()
    if dialect == "sqlite":
        _sqlite_rebuild_users(global_unique_username=True)
        return
    _downgrade_non_sqlite()

