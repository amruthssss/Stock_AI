"""SQLAlchemy models for the first durable StockWise records."""

from __future__ import annotations

from datetime import datetime
from datetime import date as DateValue

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base metadata shared by application models and Alembic."""


class ForecastRun(Base):
    """One completed chronological forecast evaluation and its source metadata."""

    __tablename__ = "forecast_runs"
    __table_args__ = (
        CheckConstraint("status = 'completed'", name="ck_forecast_runs_status"),
        CheckConstraint("schema_version > 0", name="ck_forecast_runs_schema_version"),
        CheckConstraint("series_count > 0", name="ck_forecast_runs_series_count"),
        CheckConstraint(
            "total_matching_series IS NULL OR total_matching_series >= series_count",
            name="ck_forecast_runs_total_series",
        ),
        CheckConstraint(
            "series_limit IS NULL OR series_limit > 0", name="ck_forecast_runs_series_limit"
        ),
        Index("ix_forecast_runs_generated_at_utc", "generated_at_utc"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    m5_ingestion_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state_id: Mapped[str] = mapped_column(String(8), nullable=False)
    category_id: Mapped[str] = mapped_column(String(32), nullable=False)
    series_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_matching_series: Mapped[int | None] = mapped_column(Integer)
    series_limit: Mapped[int | None] = mapped_column(Integer)
    selection_method: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    metrics: Mapped[list["ForecastMetric"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class ForecastMetric(Base):
    """One model's aggregate metrics for a rolling or held-out evaluation period."""

    __tablename__ = "forecast_metrics"
    __table_args__ = (
        CheckConstraint(
            "evaluation_period IN ('rolling_origin', 'held_out')",
            name="ck_forecast_metrics_period",
        ),
        CheckConstraint("mae >= 0", name="ck_forecast_metrics_mae"),
        CheckConstraint("wape IS NULL OR wape >= 0", name="ck_forecast_metrics_wape"),
        CheckConstraint("observations > 0", name="ck_forecast_metrics_observations"),
        Index("ix_forecast_metrics_period_model", "evaluation_period", "model_name"),
    )

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("forecast_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evaluation_period: Mapped[str] = mapped_column(String(20), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    mae: Mapped[float] = mapped_column(Float, nullable=False)
    wape: Mapped[float | None] = mapped_column(Float)
    bias: Mapped[float | None] = mapped_column(Float)
    observations: Mapped[int] = mapped_column(Integer, nullable=False)

    run: Mapped[ForecastRun] = relationship(back_populates="metrics")


class InventoryRiskRun(Base):
    """One persisted inventory-risk report derived from a synthetic snapshot."""

    __tablename__ = "inventory_risk_runs"
    __table_args__ = (
        CheckConstraint("status = 'completed'", name="ck_inventory_risk_runs_status"),
        CheckConstraint("schema_version > 0", name="ck_inventory_risk_runs_schema_version"),
        CheckConstraint("service_level > 0 AND service_level < 1", name="ck_inventory_risk_runs_service_level"),
        CheckConstraint("series_count > 0", name="ck_inventory_risk_runs_series_count"),
        CheckConstraint(
            "total_matching_series IS NULL OR total_matching_series >= series_count",
            name="ck_inventory_risk_runs_total_series",
        ),
        CheckConstraint(
            "series_limit IS NULL OR series_limit > 0", name="ck_inventory_risk_runs_series_limit"
        ),
        Index("ix_inventory_risk_runs_generated_at_utc", "generated_at_utc"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    simulation_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state_id: Mapped[str] = mapped_column(String(8), nullable=False)
    category_id: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of_date: Mapped[DateValue] = mapped_column(Date, nullable=False)
    series_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_matching_series: Mapped[int | None] = mapped_column(Integer)
    series_limit: Mapped[int | None] = mapped_column(Integer)
    selection_method: Mapped[str] = mapped_column(Text, nullable=False)
    service_level: Mapped[float] = mapped_column(Float, nullable=False)
    overstock_review_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=28, server_default="28"
    )
    forecast_model: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_horizon_description: Mapped[str] = mapped_column(Text, nullable=False)
    inventory_provenance: Mapped[str] = mapped_column(String(100), nullable=False)
    demand_reference: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    items: Mapped[list["InventoryRiskItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class InventoryRiskItem(Base):
    """One series' deterministic stockout, overstock, reorder, coverage, and expiry indicators."""

    __tablename__ = "inventory_risk_items"
    __table_args__ = (
        CheckConstraint("service_level > 0 AND service_level < 1", name="ck_inventory_risk_items_service_level"),
        CheckConstraint("forecast_days_available > 0", name="ck_inventory_risk_items_forecast_days"),
        CheckConstraint("lead_time_days > 0", name="ck_inventory_risk_items_lead_time"),
        CheckConstraint("stockout_probability >= 0 AND stockout_probability <= 1", name="ck_inventory_risk_items_probability"),
        CheckConstraint("reorder_point_units >= 0", name="ck_inventory_risk_items_reorder_point"),
        CheckConstraint("suggested_order_quantity_units >= 0", name="ck_inventory_risk_items_order_quantity"),
        CheckConstraint("expiry_units_at_risk >= 0", name="ck_inventory_risk_items_expiry_units"),
        CheckConstraint("overstock_target_units >= 0", name="ck_inventory_risk_items_overstock_target"),
        CheckConstraint("overstock_units >= 0", name="ck_inventory_risk_items_overstock_units"),
        CheckConstraint("expiry_value_at_risk_proxy >= 0", name="ck_inventory_risk_items_expiry_value"),
        Index("ix_inventory_risk_items_probability", "stockout_probability"),
    )

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("inventory_risk_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    as_of_date: Mapped[DateValue] = mapped_column(Date, nullable=False)
    service_level: Mapped[float] = mapped_column(Float, nullable=False)
    forecast_days_available: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_average_daily_units: Mapped[float] = mapped_column(Float, nullable=False)
    stock_coverage_days: Mapped[float | None] = mapped_column(Float)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_lead_time_demand_units: Mapped[float] = mapped_column(Float, nullable=False)
    lead_time_demand_std_units: Mapped[float] = mapped_column(Float, nullable=False)
    lead_time_demand_quantile_units: Mapped[float] = mapped_column(Float, nullable=False)
    safety_stock_units: Mapped[float] = mapped_column(Float, nullable=False)
    reorder_point_units: Mapped[int] = mapped_column(Integer, nullable=False)
    incoming_units_within_lead_time: Mapped[int] = mapped_column(Integer, nullable=False)
    available_supply_at_lead_time: Mapped[int] = mapped_column(Integer, nullable=False)
    stockout_probability: Mapped[float] = mapped_column(Float, nullable=False)
    stockout_exposure_units: Mapped[int] = mapped_column(Integer, nullable=False)
    suggested_order_quantity_units: Mapped[int] = mapped_column(Integer, nullable=False)
    overstock_target_units: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    overstock_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    expiry_units_at_risk: Mapped[float] = mapped_column(Float, nullable=False)
    expiry_value_at_risk_proxy: Mapped[float] = mapped_column(Float, nullable=False)

    run: Mapped[InventoryRiskRun] = relationship(back_populates="items")


class InvestigationAudit(Base):
    """Sanitized metadata for one investigation; intentionally excludes content."""

    __tablename__ = "investigation_audits"
    __table_args__ = (
        CheckConstraint(
            "evidence_type IN ('forecast', 'inventory-risk', 'decision', 'business', 'multi-domain')",
            name="ck_investigation_audits_evidence_type",
        ),
        CheckConstraint(
            "status IN ('completed', 'not_found', 'validation_failed', 'needs_clarification')",
            name="ck_investigation_audits_status",
        ),
        CheckConstraint(
            "evaluation_period IS NULL OR evaluation_period IN ('rolling_origin', 'held_out')",
            name="ck_investigation_audits_period",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_investigation_audits_duration"),
        CheckConstraint("ollama_call_count >= 0", name="ck_investigation_audits_ollama_calls"),
        CheckConstraint("llm_inference_duration_ms >= 0", name="ck_investigation_audits_llm_duration"),
        Index("ix_investigation_audits_created_at_utc", "created_at_utc"),
    )

    investigation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_period: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    explanation_source: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(160))
    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False)
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    numeric_claims_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_references_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    agent_output_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_domains: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_run_ids: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False, default=dict)
    selected_agents: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    agent_statuses: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    ollama_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    llm_inference_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fallback_reason: Mapped[str | None] = mapped_column(String(200))


class InventoryRiskApproval(Base):
    """Append-only human review event for a saved replenishment recommendation."""

    __tablename__ = "inventory_risk_approvals"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'revision_requested')",
            name="ck_inventory_risk_approvals_decision",
        ),
        CheckConstraint(
            "recommended_order_quantity_units > 0",
            name="ck_inventory_risk_approvals_order_quantity",
        ),
        CheckConstraint(
            "stockout_probability >= 0 AND stockout_probability <= 1",
            name="ck_inventory_risk_approvals_probability",
        ),
        ForeignKeyConstraint(
            ["risk_run_id", "series_id"],
            ["inventory_risk_items.run_id", "inventory_risk_items.series_id"],
            ondelete="RESTRICT",
            name="fk_inventory_risk_approvals_source_item",
        ),
        Index("ix_inventory_risk_approvals_created_at_utc", "created_at_utc"),
        Index("ix_inventory_risk_approvals_risk_run_id", "risk_run_id"),
    )

    approval_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    risk_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    recommended_order_quantity_units: Mapped[int] = mapped_column(Integer, nullable=False)
    stockout_probability: Mapped[float] = mapped_column(Float, nullable=False)
    inventory_provenance: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)


class BackgroundJob(Base):
    """PostgreSQL-backed, leased work item for bounded or long-running local tasks."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_background_jobs_attempt_count"),
        CheckConstraint("max_attempts BETWEEN 1 AND 5", name="ck_background_jobs_max_attempts"),
        Index("ix_background_jobs_queue", "status", "available_at", "created_at_utc"),
        Index("ix_background_jobs_lease", "status", "lease_expires_at"),
    )

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(200))
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationRequest(Base):
    """Durable idempotency record for authenticated external automation requests."""

    __tablename__ = "integration_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_integration_requests_status",
        ),
        Index("ix_integration_requests_created_at_utc", "created_at_utc"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmailDeliveryLog(Base):
    """Sanitized delivery state; message bodies and SMTP credentials are never stored."""

    __tablename__ = "email_delivery_logs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'sent', 'dry_run', 'failed')",
            name="ck_email_delivery_logs_status",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_email_delivery_logs_attempts"),
        Index("ix_email_delivery_logs_created_at_utc", "created_at_utc"),
    )

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    report_type: Mapped[str] = mapped_column(String(40), nullable=False)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(40))
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
