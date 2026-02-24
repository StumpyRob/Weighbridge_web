"""relax legacy printing not-null columns for compatibility

Revision ID: a1b2c3d4e5f6
Revises: z0a1b2c3d4e5
Create Date: 2026-02-24 21:55:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(col["name"]) for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "print_templates"):
        columns = _column_names(inspector, "print_templates")
        if {"document_type", "purpose"}.issubset(columns):
            bind.execute(
                sa.text(
                    """
                    UPDATE print_templates
                    SET purpose = COALESCE(NULLIF(trim(purpose), ''), document_type)
                    """
                )
            )
        if {"format", "content_type"}.issubset(columns):
            bind.execute(
                sa.text(
                    """
                    UPDATE print_templates
                    SET content_type = COALESCE(NULLIF(trim(content_type), ''), format)
                    """
                )
            )
        with op.batch_alter_table("print_templates") as batch_op:
            if "purpose" in columns:
                batch_op.alter_column(
                    "purpose",
                    existing_type=sa.String(length=32),
                    nullable=True,
                )
            if "content_type" in columns:
                batch_op.alter_column(
                    "content_type",
                    existing_type=sa.String(length=16),
                    nullable=True,
                )
            if "code" in columns:
                batch_op.alter_column(
                    "code",
                    existing_type=sa.String(length=50),
                    nullable=True,
                )

    inspector = sa.inspect(bind)
    if _table_exists(inspector, "print_jobs"):
        columns = _column_names(inspector, "print_jobs")
        if {"document_type", "purpose"}.issubset(columns):
            bind.execute(
                sa.text(
                    """
                    UPDATE print_jobs
                    SET purpose = COALESCE(NULLIF(trim(purpose), ''), document_type)
                    """
                )
            )
        if {"delivery_type", "transport_mode"}.issubset(columns):
            bind.execute(
                sa.text(
                    """
                    UPDATE print_jobs
                    SET transport_mode = COALESCE(NULLIF(trim(transport_mode), ''), delivery_type)
                    """
                )
            )
        with op.batch_alter_table("print_jobs") as batch_op:
            if "purpose" in columns:
                batch_op.alter_column(
                    "purpose",
                    existing_type=sa.String(length=32),
                    nullable=True,
                )
            if "transport_mode" in columns:
                batch_op.alter_column(
                    "transport_mode",
                    existing_type=sa.String(length=32),
                    nullable=True,
                )


def downgrade() -> None:
    # Compatibility migration; no strict downgrade required.
    pass

