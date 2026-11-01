"""Read-only API for governed forecast and inventory-risk investigations."""

from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as ApiPath
from sqlalchemy import Engine

from stockwise.agent.investigation import (
    load_investigation_checkpoint,
    run_investigation,
)
from stockwise.api.dependencies import get_database_engine
from stockwise.api.schemas import (
    InvestigationAuditListResponse,
    InvestigationRequest,
    InvestigationResponse,
)
from stockwise.persistence.investigation_audits import (
    list_investigation_audits,
    record_investigation_audit,
)

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


@router.post("", response_model=InvestigationResponse)
def investigate(
    request: InvestigationRequest,
    http_request: Request,
    engine: Engine = Depends(get_database_engine),  # noqa: B008 - FastAPI dependency injection.
) -> InvestigationResponse:
    """Summarize one saved run using only explicitly approved read-only evidence."""
    started_at = perf_counter()
    result = run_investigation(
        engine,
        evidence_type=request.evidence_type,
        run_id=request.run_id,
        evaluation_period=request.evaluation_period,
        checkpointer=http_request.app.state.investigation_checkpointer,
        question=request.question,
        domains=request.domains,
        run_ids=request.run_ids,
        decision_scenarios=request.decision_scenarios,
        include_business_intelligence=request.include_business_intelligence,
        settings=http_request.app.state.settings,
    )
    evidence_type = result.get("evidence_type", request.evidence_type or "business")
    source_run_id = next(
        (
            run_id
            for run_ids in result.get("source_run_ids", {}).values()
            for run_id in run_ids
        ),
        request.run_id or "multiple",
    )
    record_investigation_audit(
        engine,
        result=result,
        evidence_type=evidence_type,
        source_run_id=source_run_id,
        evaluation_period=(
            request.evaluation_period if "forecast" in result.get("selected_domains", []) else None
        ),
        duration_ms=int((perf_counter() - started_at) * 1000),
    )
    return _response_from_state(result, evidence_type)


@router.get("", response_model=InvestigationAuditListResponse)
def list_investigations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_database_engine),  # noqa: B008 - FastAPI dependency injection.
) -> InvestigationAuditListResponse:
    """List sanitized run metadata without exposing answer or evidence content."""
    records, total = list_investigation_audits(engine, limit=limit, offset=offset)
    return InvestigationAuditListResponse(
        items=records,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{investigation_id}", response_model=InvestigationResponse)
def get_investigation(
    http_request: Request,
    investigation_id: str = ApiPath(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$"),
    engine: Engine = Depends(get_database_engine),  # noqa: B008 - FastAPI dependency injection.
) -> InvestigationResponse:
    """Read a previously checkpointed investigation by its opaque ID."""
    result = load_investigation_checkpoint(
        engine,
        http_request.app.state.investigation_checkpointer,
        investigation_id,
        settings=http_request.app.state.settings,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return _response_from_state(result, result.get("evidence_type", "forecast"))


def _response_from_state(result: dict, evidence_type: str) -> InvestigationResponse:
    """Map internal graph state to the deliberately small public response."""
    return InvestigationResponse(
        investigation_id=result["investigation_id"],
        status=result["status"],
        evidence_type=evidence_type,
        answer=result["answer"],
        explanation_source=result.get("explanation_source", "deterministic_template"),
        provider=result.get("provider", "none"),
        model_name=result.get("model_name"),
        llm_used=bool(result.get("llm_used", False)),
        llm_fallback_reason=result.get("llm_fallback_reason"),
        evidence=result.get("evidence", []),
        tool_history=result.get("tool_history", []),
        validation={
            "schema_valid": result.get("schema_valid", True),
            "numeric_claims_valid": result.get("numeric_claims_valid", True),
            "evidence_references_valid": result.get("evidence_references_valid", False),
            "agent_output_valid": result.get("agent_output_valid", True),
        },
        selected_domains=result.get("selected_domains", []),
        source_run_ids=result.get("source_run_ids", {}),
        selected_agents=result.get("selected_agents", []),
        agent_results=result.get("agent_results", []),
        ollama_call_count=result.get("ollama_call_count", 0),
        llm_inference_duration_ms=result.get("llm_inference_duration_ms", 0),
        database_retrieval_duration_ms=result.get("database_retrieval_duration_ms", 0),
    )
