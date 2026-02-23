"""add company logo path fields

Revision ID: x4y5z6a7b8c9
Revises: w3x4y5z6a7b8
Create Date: 2026-02-22 23:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "x4y5z6a7b8c9"
down_revision = "w3x4y5z6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("company_settings")}

    if "company_logo_path" not in column_names:
        op.add_column(
            "company_settings",
            sa.Column("company_logo_path", sa.String(length=500), nullable=True),
        )
    if "company_logo_updated_at" not in column_names:
        op.add_column(
            "company_settings",
            sa.Column("company_logo_updated_at", sa.DateTime(), nullable=True),
        )

    # Keep existing installs unchanged by migrating legacy logo values into
    # company_logo_path when the new field is empty.
    refreshed_inspector = sa.inspect(bind)
    current_columns = {
        column["name"] for column in refreshed_inspector.get_columns("company_settings")
    }
    if "company_logo_path" not in current_columns:
        return

    company_settings = sa.table(
        "company_settings",
        sa.column("id", sa.Integer()),
        sa.column("company_logo_path", sa.String(length=500)),
        sa.column("company_logo_updated_at", sa.DateTime()),
        sa.column("logo_url", sa.String(length=500)),
        sa.column("logo_file_path", sa.String(length=500)),
    )

    select_columns = [
        company_settings.c.id,
        company_settings.c.company_logo_path,
    ]
    has_logo_url = "logo_url" in current_columns
    has_logo_file_path = "logo_file_path" in current_columns
    if has_logo_url:
        select_columns.append(company_settings.c.logo_url)
    if has_logo_file_path:
        select_columns.append(company_settings.c.logo_file_path)

    rows = bind.execute(sa.select(*select_columns)).mappings().all()
    for row in rows:
        current_path = str(row.get("company_logo_path") or "").strip()
        if current_path:
            continue

        replacement = ""
        if has_logo_file_path:
            legacy_file = str(row.get("logo_file_path") or "").strip().lstrip("/")
            if legacy_file:
                replacement = f"/media/{legacy_file}"
        if not replacement and has_logo_url:
            replacement = str(row.get("logo_url") or "").strip()
        if not replacement:
            continue

        bind.execute(
            company_settings.update()
            .where(company_settings.c.id == int(row["id"]))
            .values(
                company_logo_path=replacement,
                company_logo_updated_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("company_settings")}
    if "company_logo_updated_at" in column_names:
        op.drop_column("company_settings", "company_logo_updated_at")
    if "company_logo_path" in column_names:
        op.drop_column("company_settings", "company_logo_path")
