"""normalize user roles

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-05 21:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE users
            SET role = 'superadmin',
                tenant_id = NULL
            WHERE role IS NOT NULL
              AND lower(trim(role)) = 'superadmin'
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE users
            SET role = 'tenant_admin'
            WHERE role IS NOT NULL
              AND lower(trim(role)) IN ('admin', 'tenant_admin')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE users
            SET role = 'user'
            WHERE role IS NOT NULL
              AND lower(trim(role)) IN ('operator', 'user')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE users
            SET role = CASE
                WHEN tenant_id IS NULL THEN 'superadmin'
                ELSE 'tenant_admin'
            END
            WHERE role IS NULL
               OR trim(role) = ''
               OR lower(trim(role)) NOT IN ('superadmin', 'tenant_admin', 'user')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE users
            SET tenant_id = NULL
            WHERE lower(trim(role)) = 'superadmin'
            """
        )
    )


def downgrade() -> None:
    pass
