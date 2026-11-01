"""Create sanitized investigation audit history."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_01"
down_revision: Union[str, None] = "20261029_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "investigation_audits",
        sa.Column("investigation_id", sa.String(length=32), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_type", sa.String(length=20), nullable=False),
        sa.Column("source_run_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_period", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("explanation_source", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=True),
        sa.Column("llm_used", sa.Boolean(), nullable=False),
        sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("numeric_claims_valid", sa.Boolean(), nullable=False),
        sa.Column("evidence_references_valid", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "evidence_type IN ('forecast', 'inventory-risk')",
            name="ck_investigation_audits_evidence_type",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'not_found', 'validation_failed')",
            name="ck_investigation_audits_status",
        ),
        sa.CheckConstraint(
            "evaluation_period IS NULL OR evaluation_period IN ('rolling_origin', 'held_out')",
            name="ck_investigation_audits_period",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_investigation_audits_duration"),
        sa.PrimaryKeyConstraint("investigation_id", name="pk_investigation_audits"),
    )
    op.create_index(
        "ix_investigation_audits_created_at_utc",
        "investigation_audits",
        ["created_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_audits_created_at_utc", table_name="investigation_audits")
    op.drop_table("investigation_audits")
