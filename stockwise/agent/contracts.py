"""Validated request and response contracts for local specialist agents."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceDomain = Literal["forecast", "inventory-risk", "decision"]
AgentName = Literal[
    "forecast_intelligence",
    "inventory_risk",
    "decision_intelligence",
    "business_intelligence",
]

_DIGIT = re.compile(r"\d")
_NUMBER_WORD = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"half|halves|quarter|quarters|double|doubled|twice|pair|couple|both|few|"
    r"several|dozen|dozens|hundreds|thousands|majority|minority)\b",
    re.IGNORECASE,
)


class InvestigationRoute(BaseModel):
    """Only allowlisted domains may be selected by the local supervisor."""

    model_config = ConfigDict(extra="forbid")
    domains: list[EvidenceDomain] = Field(min_length=1, max_length=3)
    decision_scenarios: list[Literal["markdown", "purchase_optimization", "policy_benchmark"]] = Field(
        default_factory=list, max_length=3
    )
    include_business_intelligence: bool = False
    reason: Literal["single_domain", "decision_question", "cross_domain"]


class AgentFinding(BaseModel):
    """One qualitative observation tied to one or more evidence packet IDs."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=180)
    evidence_refs: list[str] = Field(min_length=1, max_length=2)


class SpecialistResponse(BaseModel):
    """LLM-authored qualitative interpretation; numbers stay in backend facts."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=180)
    findings: list[AgentFinding] = Field(min_length=1, max_length=1)
    assumptions: list[str] = Field(default_factory=list, max_length=6)
    missing_data: list[str] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(min_length=1, max_length=1)
    confidence_basis: str = Field(
        default="This interpretation uses only the supplied saved evidence.",
        min_length=1,
        max_length=120,
    )


class DecisionNumericReference(BaseModel):
    """A model-selected pointer to a numeric fact; the model never returns its value."""

    model_config = ConfigDict(extra="forbid")
    fact_ref: str = Field(min_length=1, max_length=32)
    evidence_ref: str = Field(min_length=1, max_length=32)


DecisionNote = Annotated[str, Field(min_length=1, max_length=72)]


class DecisionSpecialistResponse(BaseModel):
    """Qualitative decision analysis plus pointers to app-verified saved numbers."""

    model_config = ConfigDict(extra="forbid")
    decision_summary: DecisionNote
    evidence_refs: list[str] = Field(min_length=1, max_length=2)
    numerical_finding_refs: list[DecisionNumericReference] = Field(min_length=1, max_length=3)
    explanation: DecisionNote


class ValidatedSpecialistResult(BaseModel):
    """Agent result plus validator-owned status and bounded inference telemetry."""

    agent: AgentName
    status: Literal["completed", "fallback", "failed"]
    summary: str
    findings: list[AgentFinding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence_basis: str | None = None
    evidence_refs_valid: bool
    numeric_claims_valid: bool
    call_made: bool = False
    failure_reason: str | None = None
    model_name: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    ollama_total_duration_ns: int | None = Field(default=None, ge=0)
    ollama_load_duration_ns: int | None = Field(default=None, ge=0)
    ollama_prompt_eval_duration_ns: int | None = Field(default=None, ge=0)
    ollama_eval_duration_ns: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


def contains_numeric_claim(text: str) -> bool:
    """Reject quantities in free-form agent prose; deterministic code writes numbers."""
    return bool(_DIGIT.search(text) or _NUMBER_WORD.search(text))


def validate_specialist_response(
    response: SpecialistResponse,
    *,
    allowed_evidence_ids: set[str],
) -> tuple[bool, bool]:
    """Check all citations and ensure qualitative text does not introduce quantities."""
    prose = [response.summary, response.confidence_basis]
    prose.extend(finding.text for finding in response.findings)
    prose.extend(response.assumptions)
    prose.extend(response.missing_data)
    prose.extend(response.limitations)
    numeric_valid = not any(contains_numeric_claim(value) for value in prose)
    references = [reference for finding in response.findings for reference in finding.evidence_refs]
    references_valid = bool(references) and all(reference in allowed_evidence_ids for reference in references)
    return references_valid, numeric_valid
