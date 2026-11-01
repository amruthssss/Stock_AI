"""Pydantic response schemas for persisted forecast runs."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ForecastMetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evaluation_period: Literal["rolling_origin", "held_out"]
    model_name: str
    mae: float
    wape: float | None
    bias: float | None
    observations: int


class ForecastRunSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    generated_at_utc: datetime
    status: Literal["completed"]
    schema_version: int
    source: str
    m5_ingestion_run_id: str
    state_id: str
    category_id: str
    series_count: int
    total_matching_series: int | None
    series_limit: int | None
    selection_method: str


class ForecastRunListResponse(BaseModel):
    items: list[ForecastRunSummaryResponse]
    total: int
    limit: int
    offset: int


class ForecastRunDetailResponse(ForecastRunSummaryResponse):
    metrics: list[ForecastMetricResponse]


class ForecastLeaderboardItemResponse(ForecastMetricResponse):
    rank: int
    sort_value: float | None


class ForecastLeaderboardResponse(BaseModel):
    run_id: str
    evaluation_period: Literal["rolling_origin", "held_out"]
    sort_by: Literal["mae", "wape", "bias"]
    items: list[ForecastLeaderboardItemResponse]


class ReportArtifactSummaryResponse(BaseModel):
    report_id: str
    report_type: Literal["daily_inventory", "weekly_data_science"]
    status: Literal["completed"]
    generated_at_utc: datetime
    source_run_id: str | None
    summary: dict[str, Any]
    data_provenance: dict[str, Any]
    limitations: str
    html_available: bool


class ReportArtifactListResponse(BaseModel):
    items: list[ReportArtifactSummaryResponse]
    total: int
    limit: int
    offset: int


class ReportArtifactDetailResponse(ReportArtifactSummaryResponse):
    selection: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    interpretation: dict[str, Any] | None = None
    metric_explanations: list[dict[str, Any]] | None = None
    action_plan: list[dict[str, Any]] | None = None
    agent_intelligence: dict[str, Any] | None = None


class InventoryRiskRunSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    generated_at_utc: datetime
    status: Literal["completed"]
    schema_version: int
    simulation_run_id: str
    state_id: str
    category_id: str
    as_of_date: date
    series_count: int
    total_matching_series: int | None
    series_limit: int | None
    selection_method: str
    service_level: float
    overstock_review_period_days: int
    forecast_model: str
    forecast_horizon_description: str
    inventory_provenance: str
    demand_reference: str


class InventoryRiskListResponse(BaseModel):
    items: list[InventoryRiskRunSummaryResponse]
    total: int
    limit: int
    offset: int


class InventoryRiskItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    series_id: str
    as_of_date: date
    service_level: float
    forecast_days_available: int
    forecast_average_daily_units: float
    stock_coverage_days: float | None
    lead_time_days: int
    expected_lead_time_demand_units: float
    lead_time_demand_std_units: float
    lead_time_demand_quantile_units: float
    safety_stock_units: float
    reorder_point_units: int
    incoming_units_within_lead_time: int
    available_supply_at_lead_time: int
    overstock_target_units: int
    overstock_units: int
    stockout_probability: float
    stockout_exposure_units: int
    suggested_order_quantity_units: int
    expiry_units_at_risk: float
    expiry_value_at_risk_proxy: float


class InventoryRiskDetailResponse(InventoryRiskRunSummaryResponse):
    results: list[InventoryRiskItemResponse]


class InvestigationRequest(BaseModel):
    """Bounded request for a read-only, evidence-backed investigation."""

    evidence_type: Literal["forecast", "inventory-risk"] | None = None
    run_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    domains: list[Literal["forecast", "inventory-risk", "decision"]] | None = Field(
        default=None, min_length=1, max_length=3
    )
    run_ids: dict[str, str] = Field(default_factory=dict, max_length=3)
    decision_scenarios: list[Literal["markdown", "purchase_optimization", "policy_benchmark"]] = Field(
        default_factory=list, max_length=3
    )
    include_business_intelligence: bool = False
    evaluation_period: Literal["rolling_origin", "held_out"] = "held_out"
    question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_request_shape(self) -> InvestigationRequest:
        if self.evidence_type is not None and self.run_id is None:
            raise ValueError("run_id is required when evidence_type is specified")
        if self.evidence_type is None and self.question is None:
            raise ValueError("question is required for supervisor-routed investigations")
        if self.domains is not None and len(set(self.domains)) != len(self.domains):
            raise ValueError("domains must not contain duplicates")
        if len(set(self.decision_scenarios)) != len(self.decision_scenarios):
            raise ValueError("decision_scenarios must not contain duplicates")
        allowed_domains = {"forecast", "inventory-risk", "decision"}
        for domain, run_id in self.run_ids.items():
            if domain not in allowed_domains:
                raise ValueError("run_ids contains an unsupported domain")
            if not run_id or len(run_id) > 64 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id
            ):
                raise ValueError("run_ids contains an invalid run identifier")
        return self


class InvestigationEvidenceResponse(BaseModel):
    domain: Literal["forecast", "inventory-risk", "decision"] | None = None
    evidence_id: str
    source_table: str
    source_run_id: str
    facts: dict[str, object]


class InvestigationToolCallResponse(BaseModel):
    tool_name: str
    status: Literal["completed", "not_found", "failed"]


class InvestigationValidationResponse(BaseModel):
    schema_valid: bool
    numeric_claims_valid: bool
    evidence_references_valid: bool
    agent_output_valid: bool = True


class InvestigationResponse(BaseModel):
    investigation_id: str
    status: Literal["completed", "not_found", "validation_failed", "needs_clarification"]
    evidence_type: Literal["forecast", "inventory-risk", "decision", "business", "multi-domain"]
    answer: str
    # Keep the former OmniRoute value readable for investigations saved before the switch.
    explanation_source: Literal[
        "deterministic_template",
        "deterministic_template+omniroute",
        "deterministic_template+ollama",
        "deterministic_template+ollama_agents",
    ]
    provider: str
    model_name: str | None
    llm_used: bool
    llm_fallback_reason: str | None = None
    selected_domains: list[str] = Field(default_factory=list)
    source_run_ids: dict[str, list[str]] = Field(default_factory=dict)
    selected_agents: list[str] = Field(default_factory=list)
    agent_results: list[dict[str, object]] = Field(default_factory=list)
    ollama_call_count: int = 0
    llm_inference_duration_ms: int = 0
    database_retrieval_duration_ms: int = 0
    evidence: list[InvestigationEvidenceResponse]
    tool_history: list[InvestigationToolCallResponse]
    validation: InvestigationValidationResponse


class InvestigationAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    investigation_id: str
    created_at_utc: datetime
    evidence_type: Literal["forecast", "inventory-risk", "decision", "business", "multi-domain"]
    source_run_id: str
    evaluation_period: Literal["rolling_origin", "held_out"] | None
    status: Literal["completed", "not_found", "validation_failed", "needs_clarification"]
    explanation_source: str
    provider: str
    model_name: str | None
    llm_used: bool
    schema_valid: bool
    numeric_claims_valid: bool
    evidence_references_valid: bool
    duration_ms: int
    selected_domains: list[str] = Field(default_factory=list)
    source_run_ids: dict[str, list[str]] = Field(default_factory=dict)
    selected_agents: list[str] = Field(default_factory=list)
    agent_statuses: dict[str, str] = Field(default_factory=dict)
    ollama_call_count: int = 0
    llm_inference_duration_ms: int = 0
    agent_output_valid: bool = True
    fallback_reason: str | None = None


class InvestigationAuditListResponse(BaseModel):
    items: list[InvestigationAuditResponse]
    total: int
    limit: int
    offset: int


class InventoryRiskApprovalRequest(BaseModel):
    """A human decision about the saved suggested order for one risk item."""

    risk_run_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    series_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "rejected", "revision_requested"]
    note: str | None = Field(default=None, max_length=1000)


class InventoryRiskApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    created_at_utc: datetime
    risk_run_id: str
    series_id: str
    decision: Literal["approved", "rejected", "revision_requested"]
    note: str | None
    recommended_order_quantity_units: int
    stockout_probability: float
    inventory_provenance: str
    actor: str


class InventoryRiskApprovalListResponse(BaseModel):
    items: list[InventoryRiskApprovalResponse]
    total: int
    limit: int
    offset: int


class DecisionLabRunRequest(BaseModel):
    """Bounded request to run one saved-data decision scenario."""

    scenario: Literal["markdown", "purchase_optimization", "policy_benchmark"]
    budget: float | None = Field(default=None, ge=0, le=1_000_000)
    max_seconds: float = Field(default=5.0, ge=0.1, le=10.0)


class DecisionLabRunResponse(BaseModel):
    scenario: Literal["markdown", "purchase_optimization", "policy_benchmark"]
    run_id: str
    status: str
    report: dict[str, object]


class DecisionLabLatestResponse(BaseModel):
    reports: dict[str, DecisionLabRunResponse | None]


class BackgroundJobResponse(BaseModel):
    """Safe job status returned by the API; never includes the idempotency key."""

    job_id: str
    job_type: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempt_count: int
    max_attempts: int
    created_at_utc: datetime
    updated_at_utc: datetime
    result: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None


class BackgroundJobSummaryResponse(BaseModel):
    """Small history row without the full saved result or request payload."""

    job_id: str
    job_type: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempt_count: int
    max_attempts: int
    created_at_utc: datetime
    updated_at_utc: datetime
    error_code: str | None = None
    error_message: str | None = None


class BackgroundJobListResponse(BaseModel):
    items: list[BackgroundJobSummaryResponse]
    total: int
    limit: int
    offset: int
