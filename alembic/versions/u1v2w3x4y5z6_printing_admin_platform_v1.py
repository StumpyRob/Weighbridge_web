"""printing admin platform v1

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-02-21 10:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "u1v2w3x4y5z6"
down_revision = "t0u1v2w3x4y5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "print_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("code", name="uq_print_templates_code"),
    )
    op.create_index(
        "ix_print_templates_purpose",
        "print_templates",
        ["purpose"],
        unique=False,
    )
    op.create_index(
        "ix_print_templates_is_active",
        "print_templates",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "print_template_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("print_templates.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_print_template_versions_template_id",
        "print_template_versions",
        ["template_id"],
        unique=False,
    )

    op.create_table(
        "print_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("print_profiles.id"), nullable=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("print_templates.id"), nullable=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=True),
        sa.Column("transport_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "transport_config_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("rendered_content", sa.Text(), nullable=True),
        sa.Column("rendered_bytes_base64", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_print_jobs_status", "print_jobs", ["status"], unique=False)
    op.create_index("ix_print_jobs_purpose", "print_jobs", ["purpose"], unique=False)
    op.create_index("ix_print_jobs_profile_id", "print_jobs", ["profile_id"], unique=False)
    op.create_index("ix_print_jobs_template_id", "print_jobs", ["template_id"], unique=False)
    op.create_index("ix_print_jobs_ticket_id", "print_jobs", ["ticket_id"], unique=False)
    op.create_index("ix_print_jobs_created_at", "print_jobs", ["created_at"], unique=False)

    with op.batch_alter_table("print_profiles") as batch_op:
        batch_op.add_column(sa.Column("template_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("yard_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_print_profiles_template_id",
            "print_templates",
            ["template_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_print_profiles_yard_id",
            "yards",
            ["yard_id"],
            ["id"],
        )
        batch_op.create_index("ix_print_profiles_template_id", ["template_id"], unique=False)
        batch_op.create_index("ix_print_profiles_yard_id", ["yard_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("print_profiles") as batch_op:
        batch_op.drop_index("ix_print_profiles_yard_id")
        batch_op.drop_index("ix_print_profiles_template_id")
        batch_op.drop_constraint("fk_print_profiles_yard_id", type_="foreignkey")
        batch_op.drop_constraint("fk_print_profiles_template_id", type_="foreignkey")
        batch_op.drop_column("yard_id")
        batch_op.drop_column("template_id")

    op.drop_index("ix_print_jobs_created_at", table_name="print_jobs")
    op.drop_index("ix_print_jobs_ticket_id", table_name="print_jobs")
    op.drop_index("ix_print_jobs_template_id", table_name="print_jobs")
    op.drop_index("ix_print_jobs_profile_id", table_name="print_jobs")
    op.drop_index("ix_print_jobs_purpose", table_name="print_jobs")
    op.drop_index("ix_print_jobs_status", table_name="print_jobs")
    op.drop_table("print_jobs")

    op.drop_index(
        "ix_print_template_versions_template_id",
        table_name="print_template_versions",
    )
    op.drop_table("print_template_versions")

    op.drop_index("ix_print_templates_is_active", table_name="print_templates")
    op.drop_index("ix_print_templates_purpose", table_name="print_templates")
    op.drop_table("print_templates")
