"""refactor platform email to resend

Revision ID: e7f8a9b0c1d2
Revises: c9d0e1f2a3b4
Create Date: 2026-03-15 10:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "email_provider",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'resend'"),
            )
        )
        batch_op.add_column(sa.Column("resend_api_key", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("from_email", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("from_display_name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("reply_to", sa.String(length=255), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE platform_settings
            SET email_provider = COALESCE(NULLIF(trim(email_provider), ''), 'resend'),
                resend_api_key = CASE
                    WHEN resend_api_key IS NOT NULL AND trim(resend_api_key) <> '' THEN resend_api_key
                    WHEN lower(trim(coalesce(smtp_host, ''))) LIKE '%resend%'
                      OR lower(trim(coalesce(smtp_username, ''))) = 'resend'
                    THEN smtp_password
                    ELSE resend_api_key
                END,
                from_email = CASE
                    WHEN from_email IS NOT NULL AND trim(from_email) <> '' THEN from_email
                    ELSE smtp_from_email
                END,
                from_display_name = CASE
                    WHEN from_display_name IS NOT NULL AND trim(from_display_name) <> '' THEN from_display_name
                    ELSE smtp_from_display_name
                END,
                reply_to = CASE
                    WHEN reply_to IS NOT NULL AND trim(reply_to) <> '' THEN reply_to
                    ELSE smtp_reply_to
                END
            """
        )
    )

    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.alter_column("email_provider", server_default=None)
        batch_op.drop_column("smtp_security")
        batch_op.drop_column("smtp_reply_to")
        batch_op.drop_column("smtp_from_display_name")
        batch_op.drop_column("smtp_from_email")
        batch_op.drop_column("smtp_password")
        batch_op.drop_column("smtp_username")
        batch_op.drop_column("smtp_port")
        batch_op.drop_column("smtp_host")


def downgrade() -> None:
    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.add_column(sa.Column("smtp_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_port", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("smtp_username", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_password", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_from_email", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_from_display_name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("smtp_reply_to", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_security", sa.String(length=16), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE platform_settings
            SET smtp_host = CASE
                    WHEN lower(trim(coalesce(email_provider, ''))) = 'resend' THEN 'smtp.resend.com'
                    ELSE smtp_host
                END,
                smtp_port = CASE
                    WHEN lower(trim(coalesce(email_provider, ''))) = 'resend' THEN 587
                    ELSE smtp_port
                END,
                smtp_username = CASE
                    WHEN lower(trim(coalesce(email_provider, ''))) = 'resend' THEN 'resend'
                    ELSE smtp_username
                END,
                smtp_password = CASE
                    WHEN smtp_password IS NOT NULL AND trim(smtp_password) <> '' THEN smtp_password
                    ELSE resend_api_key
                END,
                smtp_from_email = CASE
                    WHEN smtp_from_email IS NOT NULL AND trim(smtp_from_email) <> '' THEN smtp_from_email
                    ELSE from_email
                END,
                smtp_from_display_name = CASE
                    WHEN smtp_from_display_name IS NOT NULL AND trim(smtp_from_display_name) <> '' THEN smtp_from_display_name
                    ELSE from_display_name
                END,
                smtp_reply_to = CASE
                    WHEN smtp_reply_to IS NOT NULL AND trim(smtp_reply_to) <> '' THEN smtp_reply_to
                    ELSE reply_to
                END,
                smtp_security = CASE
                    WHEN lower(trim(coalesce(email_provider, ''))) = 'resend' THEN 'starttls'
                    ELSE smtp_security
                END
            """
        )
    )

    with op.batch_alter_table("platform_settings") as batch_op:
        batch_op.drop_column("reply_to")
        batch_op.drop_column("from_display_name")
        batch_op.drop_column("from_email")
        batch_op.drop_column("resend_api_key")
        batch_op.drop_column("email_provider")
