"""Write and read compact investigation audit records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from backend.persistence.models import InvestigationAudit


def record_investigation_audit(
    engine: Engine,
    *,
    result: dict[str, Any],
    evidence_type: str,
    source_run_id: str,
    evaluation_period: str | None,
    duration_ms: int,
) -> None:
    """Persist references and validation metadata without prompt/answer content."""
    audit = InvestigationAudit(
        investigation_id=result["investigation_id"],
        created_at_utc=datetime.now(timezone.utc),
        evidence_type=result.get("evidence_type", evidence_type),
        source_run_id=result.get("source_run_id") or source_run_id,
        evaluation_period=evaluation_period,
        status=result["status"],
        explanation_source=result.get("explanation_source", "deterministic_template"),
        provider=result.get("provider", "none"),
        model_name=result.get("model_name"),
        llm_used=bool(result.get("llm_used", False)),
        schema_valid=bool(result.get("schema_valid", True)),
        numeric_claims_valid=bool(result.get("numeric_claims_valid", True)),
        evidence_references_valid=bool(result.get("evidence_references_valid", False)),
        agent_output_valid=bool(result.get("agent_output_valid", True)),
        duration_ms=max(0, duration_ms),
        selected_domains=result.get("selected_domains", []),
        source_run_ids=result.get("source_run_ids", {}),
        selected_agents=result.get("selected_agents", []),
        agent_statuses={
            item.get("agent", "unknown"): item.get("status", "unknown")
            for item in result.get("agent_results", [])
            if isinstance(item, dict)
        },
        ollama_call_count=max(0, int(result.get("ollama_call_count", 0))),
        llm_inference_duration_ms=max(0, int(result.get("llm_inference_duration_ms", 0))),
        fallback_reason=result.get("llm_fallback_reason"),
    )
    with Session(engine) as session:
        with session.begin():
            session.add(audit)


def list_investigation_audits(
    engine: Engine,
    *,
    limit: int,
    offset: int,
) -> tuple[list[InvestigationAudit], int]:
    """Return a bounded newest-first page of sanitized investigation records."""
    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(InvestigationAudit)) or 0
        records = session.scalars(
            select(InvestigationAudit)
            .order_by(
                InvestigationAudit.created_at_utc.desc(),
                InvestigationAudit.investigation_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return list(records), total
