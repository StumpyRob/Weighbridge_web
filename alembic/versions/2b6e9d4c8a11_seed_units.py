"""seed units

Revision ID: 2b6e9d4c8a11
Revises: 1f3c9a2b7d50
Create Date: 2026-01-23 09:10:00.000000
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "2b6e9d4c8a11"
down_revision = "1f3c9a2b7d50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.utcnow()
    rows = [
        ("TONNE", "Tonne"),
        ("KG", "Kilogram"),
        ("LOAD", "Load"),
    ]
    for code, description in rows:
        exists = conn.execute(
            sa.text("SELECT 1 FROM units WHERE code = :code LIMIT 1"),
            {"code": code},
        ).fetchone()
        if exists:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO units (code, description, is_active, created_at, updated_at) "
                "VALUES (:code, :description, :is_active, :created_at, :updated_at)"
            ),
            {
                "code": code,
                "description": description,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM units WHERE code IN ('TONNE', 'KG', 'LOAD')")
    )
