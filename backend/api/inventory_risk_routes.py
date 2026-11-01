"""Read-only endpoints for persisted inventory-risk runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from backend.api.dependencies import get_database_engine
from backend.api.schemas import (
    InventoryRiskDetailResponse,
    InventoryRiskItemResponse,
    InventoryRiskListResponse,
    InventoryRiskRunSummaryResponse,
)
from backend.persistence.models import InventoryRiskItem, InventoryRiskRun


router = APIRouter(prefix="/api/v1/inventory-risks", tags=["inventory risks"])


def _run_or_404(session: Session, run_id: str) -> InventoryRiskRun:
    run = session.get(InventoryRiskRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Inventory-risk run not found")
    return run


@router.get("", response_model=InventoryRiskListResponse)
def list_inventory_risk_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_database_engine),
) -> InventoryRiskListResponse:
    """List newest imported risk runs with bounded pagination."""
    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(InventoryRiskRun)) or 0
        runs = session.scalars(
            select(InventoryRiskRun)
            .order_by(InventoryRiskRun.generated_at_utc.desc(), InventoryRiskRun.run_id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [InventoryRiskRunSummaryResponse.model_validate(run) for run in runs]
    return InventoryRiskListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=InventoryRiskDetailResponse)
def get_inventory_risk_run(
    run_id: str,
    engine: Engine = Depends(get_database_engine),
) -> InventoryRiskDetailResponse:
    """Return one risk run and its per-series metrics, highest stockout probability first."""
    with Session(engine) as session:
        run = _run_or_404(session, run_id)
        results = session.scalars(
            select(InventoryRiskItem)
            .where(InventoryRiskItem.run_id == run_id)
            .order_by(
                InventoryRiskItem.stockout_probability.desc(),
                InventoryRiskItem.expiry_units_at_risk.desc(),
                InventoryRiskItem.series_id,
            )
        ).all()
        summary = InventoryRiskRunSummaryResponse.model_validate(run)
        result_items = [InventoryRiskItemResponse.model_validate(item) for item in results]
    return InventoryRiskDetailResponse(**summary.model_dump(), results=result_items)
