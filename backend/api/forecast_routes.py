"""Read-only endpoints for imported forecast runs and within-run comparisons."""

from __future__ import annotations

import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from backend.api.dependencies import get_database_engine
from backend.api.schemas import (
    ForecastLeaderboardItemResponse,
    ForecastLeaderboardResponse,
    ForecastMetricResponse,
    ForecastRunDetailResponse,
    ForecastRunListResponse,
    ForecastRunSummaryResponse,
)
from backend.persistence.models import ForecastMetric, ForecastRun


router = APIRouter(prefix="/api/v1/forecast-runs", tags=["forecast runs"])


def _run_or_404(session: Session, run_id: str) -> ForecastRun:
    run = session.get(ForecastRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Forecast run not found")
    return run


@router.get("", response_model=ForecastRunListResponse)
def list_forecast_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_database_engine),
) -> ForecastRunListResponse:
    """Return newest imported forecast runs with bounded pagination."""
    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(ForecastRun)) or 0
        runs = session.scalars(
            select(ForecastRun)
            .order_by(ForecastRun.generated_at_utc.desc(), ForecastRun.run_id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [ForecastRunSummaryResponse.model_validate(run) for run in runs]
    return ForecastRunListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=ForecastRunDetailResponse)
def get_forecast_run(
    run_id: str,
    engine: Engine = Depends(get_database_engine),
) -> ForecastRunDetailResponse:
    """Return one run and all its aggregate model metrics."""
    with Session(engine) as session:
        run = _run_or_404(session, run_id)
        metrics = session.scalars(
            select(ForecastMetric)
            .where(ForecastMetric.run_id == run_id)
            .order_by(ForecastMetric.evaluation_period, ForecastMetric.model_name)
        ).all()
        summary = ForecastRunSummaryResponse.model_validate(run)
        metric_items = [ForecastMetricResponse.model_validate(metric) for metric in metrics]
    return ForecastRunDetailResponse(**summary.model_dump(), metrics=metric_items)


@router.get("/{run_id}/leaderboard", response_model=ForecastLeaderboardResponse)
def get_forecast_leaderboard(
    run_id: str,
    evaluation_period: Literal["rolling_origin", "held_out"] = "held_out",
    sort_by: Literal["mae", "wape", "bias"] = "mae",
    engine: Engine = Depends(get_database_engine),
) -> ForecastLeaderboardResponse:
    """Order models from a single run by one comparable aggregate metric."""
    with Session(engine) as session:
        _run_or_404(session, run_id)
        metrics = session.scalars(
            select(ForecastMetric).where(
                ForecastMetric.run_id == run_id,
                ForecastMetric.evaluation_period == evaluation_period,
            )
        ).all()

    def sort_key(metric: ForecastMetric) -> tuple[bool, float, str]:
        value = metric.bias if sort_by == "bias" else getattr(metric, sort_by)
        if value is None:
            return True, math.inf, metric.model_name
        comparable_value = abs(value) if sort_by == "bias" else value
        return False, comparable_value, metric.model_name

    ranked_metrics = sorted(metrics, key=sort_key)
    items = [
        ForecastLeaderboardItemResponse(
            evaluation_period=metric.evaluation_period,
            model_name=metric.model_name,
            mae=metric.mae,
            wape=metric.wape,
            bias=metric.bias,
            observations=metric.observations,
            rank=index,
            sort_value=(
                abs(metric.bias)
                if sort_by == "bias" and metric.bias is not None
                else getattr(metric, sort_by)
            ),
        )
        for index, metric in enumerate(ranked_metrics, start=1)
    ]
    return ForecastLeaderboardResponse(
        run_id=run_id,
        evaluation_period=evaluation_period,
        sort_by=sort_by,
        items=items,
    )
