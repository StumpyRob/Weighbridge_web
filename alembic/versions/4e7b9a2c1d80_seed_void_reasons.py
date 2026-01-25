"""seed void reasons

Revision ID: 4e7b9a2c1d80
Revises: 2b6e9d4c8a11
Create Date: 2026-01-23 09:25:00.000000
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "4e7b9a2c1d80"
down_revision = "2b6e9d4c8a11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.utcnow()
    rows = [
        ("DUPLICATE_TICKET", "Duplicate ticket"),
        ("WRONG_VEHICLE", "Wrong vehicle"),
        ("WRONG_CUSTOMER", "Wrong customer/account"),
        ("INCORRECT_WEIGHTS", "Incorrect weights"),
        ("CANCELLED", "Cancelled transaction"),
        ("TEST_TRAINING", "Test / training ticket"),
        ("OTHER", "Other (specify)"),
    ]
    for code, description in rows:
        exists = conn.execute(
            sa.text("SELECT 1 FROM void_reasons WHERE code = :code LIMIT 1"),
            {"code": code},
        ).fetchone()
        if exists:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO void_reasons (code, description, is_active, created_at, updated_at) "
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
        sa.text(
            "DELETE FROM void_reasons WHERE code IN "
            "('DUPLICATE_TICKET','WRONG_VEHICLE','WRONG_CUSTOMER',"
            "'INCORRECT_WEIGHTS','CANCELLED','TEST_TRAINING','OTHER')"
        )
    )
