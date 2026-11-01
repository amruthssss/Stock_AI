"""Create a minimal idempotent report email delivery log."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_05"
down_revision: Union[str, None] = "20261030_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_delivery_logs",
        sa.Column("delivery_id", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("report_type", sa.String(40), nullable=False),
        sa.Column("report_id", sa.String(64), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(40), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('processing', 'sent', 'dry_run', 'failed')",
            name="ck_email_delivery_logs_status",
        ),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_email_delivery_logs_attempts"),
    )
    op.create_index(
        "ix_email_delivery_logs_created_at_utc",
        "email_delivery_logs",
        ["created_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_delivery_logs_created_at_utc", table_name="email_delivery_logs")
    op.drop_table("email_delivery_logs")
