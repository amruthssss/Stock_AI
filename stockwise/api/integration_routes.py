"""Authenticated HTTP surfaces for n8n and other machine clients."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from stockwise.api.config import PROJECT_ROOT, Settings
from stockwise.api.dependencies import get_database_engine
from stockwise.api.integration_security import (
    enforce_integration_rate_limit,
    get_idempotent_response,
    require_integration_key,
    save_idempotent_response,
)
from stockwise.api.schemas import InventoryRiskItemResponse
from stockwise.persistence.models import InventoryRiskItem, InventoryRiskRun
from stockwise.reporting import (
    build_daily_inventory_report,
    build_weekly_data_science_report,
)

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_integration_key), Depends(enforce_integration_rate_limit)],
)

ReportType = Literal["daily_inventory", "weekly_data_science"]
AlertType = Literal["stockout", "expiry", "overstock"]
_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ReportGenerationRequest(BaseModel):
    report_type: ReportType


def _report_directory(report_type: ReportType) -> Path:
    return PROJECT_ROOT / "data" / "processed" / "reports" / report_type


def _load_report(report_type: ReportType, report_id: str) -> dict[str, object]:
    if not _REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=422, detail="Invalid report identifier")
    report_path = (_report_directory(report_type) / "runs" / report_id / "report.json").resolve()
    if not report_path.is_relative_to(_report_directory(report_type).resolve()):
        raise HTTPException(status_code=422, detail="Invalid report path")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=404, detail="Report was not found") from error
    if not isinstance(report, dict) or report.get("report_id") != report_id:
        raise HTTPException(status_code=404, detail="Report was not found")
    return report


@router.post("/reports", status_code=201)
def generate_report(
    request: ReportGenerationRequest,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=16, max_length=128)
    ],
    http_request: Request,
    engine: Engine = Depends(get_database_engine),  # noqa: B008 - FastAPI dependency injection.
) -> dict[str, object]:
    """Generate one validated report using the existing report builder."""
    payload = request.model_dump(mode="json")
    previous = get_idempotent_response(
        engine, key=idempotency_key, operation="report.generate", payload=payload
    )
    if previous is not None:
        response.status_code = 200
        return previous
    settings: Settings = http_request.app.state.settings
    checkpointer = http_request.app.state.investigation_checkpointer
    builder = (
        build_daily_inventory_report
        if request.report_type == "daily_inventory"
        else build_weekly_data_science_report
    )
    report_path = builder(
        PROJECT_ROOT,
        engine=engine,
        checkpointer=checkpointer,
        settings=settings,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail="Report builder returned invalid data")
    save_idempotent_response(
        engine, key=idempotency_key, operation="report.generate", payload=payload, response=report
    )
    return report


@router.get("/reports/{report_type}/{report_id}")
def retrieve_report(report_type: ReportType, report_id: str) -> dict[str, object]:
    """Retrieve one generated JSON report without exposing arbitrary files."""
    return _load_report(report_type, report_id)


@router.get("/inventory-alerts/{alert_type}")
def inventory_alerts(
    alert_type: AlertType,
    run_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=100, ge=1, le=100),
    engine: Engine = Depends(get_database_engine),  # noqa: B008 - FastAPI dependency injection.
) -> dict[str, object]:
    """Expose saved risk fields for one alert class; calculations remain in StockWise."""
    with Session(engine) as session:
        selected_run = (
            session.get(InventoryRiskRun, run_id)
            if run_id
            else session.scalar(
                select(InventoryRiskRun).order_by(
                    InventoryRiskRun.generated_at_utc.desc(), InventoryRiskRun.run_id.desc()
                )
            )
        )
        if selected_run is None:
            raise HTTPException(status_code=404, detail="Inventory-risk run not found")
        query = (
            select(InventoryRiskItem)
            .where(InventoryRiskItem.run_id == selected_run.run_id)
            .order_by(
                InventoryRiskItem.stockout_probability.desc()
                if alert_type == "stockout"
                else (
                    InventoryRiskItem.expiry_units_at_risk.desc()
                    if alert_type == "expiry"
                    else InventoryRiskItem.overstock_units.desc()
                ),
                InventoryRiskItem.series_id,
            )
            .limit(limit)
        )
        rows = [InventoryRiskItemResponse.model_validate(item).model_dump(mode="json") for item in session.scalars(query)]
    return {
        "alert_type": alert_type,
        "run_id": selected_run.run_id,
        "generated_at_utc": selected_run.generated_at_utc,
        "inventory_provenance": selected_run.inventory_provenance,
        "limitations": "Values are saved synthetic operational indicators; expiry value is a proxy.",
        "items": rows,
    }
