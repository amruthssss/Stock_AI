"""Add bounded domain, agent, and Ollama metadata to investigation audits."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261030_06"
down_revision: Union[str, None] = "20261030_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("investigation_audits") as batch:
        batch.drop_constraint("ck_investigation_audits_evidence_type", type_="check")
        batch.drop_constraint("ck_investigation_audits_status", type_="check")
        batch.create_check_constraint(
            "ck_investigation_audits_evidence_type",
            "evidence_type IN ('forecast', 'inventory-risk', 'decision', 'business', 'multi-domain')",
        )
        batch.create_check_constraint(
            "ck_investigation_audits_status",
            "status IN ('completed', 'not_found', 'validation_failed', 'needs_clarification')",
        )
        batch.add_column(sa.Column("selected_domains", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("source_run_ids", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("selected_agents", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
        batch.add_column(sa.Column("agent_statuses", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("ollama_call_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("agent_output_valid", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("llm_inference_duration_ms", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("fallback_reason", sa.String(200), nullable=True))
        batch.create_check_constraint(
            "ck_investigation_audits_ollama_calls", "ollama_call_count >= 0"
        )
        batch.create_check_constraint(
            "ck_investigation_audits_llm_duration", "llm_inference_duration_ms >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("investigation_audits") as batch:
        batch.drop_constraint("ck_investigation_audits_ollama_calls", type_="check")
        batch.drop_constraint("ck_investigation_audits_llm_duration", type_="check")
        batch.drop_column("fallback_reason")
        batch.drop_column("llm_inference_duration_ms")
        batch.drop_column("agent_output_valid")
        batch.drop_column("ollama_call_count")
        batch.drop_column("agent_statuses")
        batch.drop_column("selected_agents")
        batch.drop_column("source_run_ids")
        batch.drop_column("selected_domains")
        batch.drop_constraint("ck_investigation_audits_evidence_type", type_="check")
        batch.drop_constraint("ck_investigation_audits_status", type_="check")
        batch.create_check_constraint(
            "ck_investigation_audits_evidence_type",
            "evidence_type IN ('forecast', 'inventory-risk')",
        )
        batch.create_check_constraint(
            "ck_investigation_audits_status",
            "status IN ('completed', 'not_found', 'validation_failed')",
        )
