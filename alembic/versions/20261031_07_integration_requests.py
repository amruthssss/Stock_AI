"""Add durable idempotency records for external integrations."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261031_07"
down_revision: Union[str, None] = "20261030_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "integration_requests",
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_integration_requests_status",
        ),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_integration_requests_created_at_utc",
        "integration_requests",
        ["created_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_requests_created_at_utc", table_name="integration_requests")
    op.drop_table("integration_requests")
