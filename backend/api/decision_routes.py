"""Bounded API for the deterministic markdown and replenishment scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import Engine

from backend.api.config import PROJECT_ROOT
from backend.api.dependencies import get_database_engine
from backend.api.schemas import (
    BackgroundJobResponse,
    DecisionLabLatestResponse,
    DecisionLabRunRequest,
    DecisionLabRunResponse,
)
from backend.api.security import require_approval_admin
from backend.persistence.jobs import JobIdempotencyConflict, enqueue_job


router = APIRouter(prefix="/api/v1/decision-lab", tags=["decision-lab"])

_REPORT_SOURCES = {
    "markdown": ("markdown_scenarios", "markdown_report"),
    "purchase_optimization": ("purchase_optimizations", "optimization_report"),
    "policy_benchmark": ("policy_benchmarks", "benchmark_report"),
}
MAX_SCENARIO_SERIES = 25


def _read_latest_report(project_root: Path, scenario: str) -> dict[str, Any] | None:
    directory_name, report_key = _REPORT_SOURCES[scenario]
    directory = (project_root / "data" / "processed" / directory_name).resolve()
    pointer_path = directory / "latest.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        relative_path = Path(pointer[report_key])
        report_path = (directory / relative_path).resolve()
        if not report_path.is_relative_to(directory):
            raise ValueError("latest report pointer resolves outside its scenario folder")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"Latest {scenario} report is unavailable or invalid") from error
    if not isinstance(report, dict) or report.get("status") not in {"completed", "optimal", "feasible", "infeasible", "unknown"}:
        raise HTTPException(status_code=503, detail=f"Latest {scenario} report is not complete")
    if report.get("run_id") != pointer.get("run_id"):
        raise HTTPException(status_code=503, detail=f"Latest {scenario} report does not match its pointer")
    return report


def _read_bounded_input(
    project_root: Path,
    *,
    directory_name: str,
    pointer_key: str,
    records_key: str,
    label: str,
) -> dict[str, Any]:
    directory = (project_root / "data" / "processed" / directory_name).resolve()
    try:
        pointer = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        path = (directory / Path(pointer[pointer_key])).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("path escapes its data directory")
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload[records_key]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail=f"A valid latest {label} run is required") from error
    if (
        not isinstance(payload, dict)
        or payload.get("status") not in {"completed", "optimal", "feasible", "infeasible", "unknown"}
        or payload.get("run_id") != pointer.get("run_id")
    ):
        raise HTTPException(status_code=409, detail=f"A valid latest {label} run is required")
    if isinstance(records, list):
        record_count = len(records)
    elif isinstance(records, dict) and isinstance(records.get("candidate_count"), int):
        record_count = records["candidate_count"]
    else:
        raise HTTPException(status_code=409, detail=f"A valid latest {label} run is required")
    if record_count < 1:
        raise HTTPException(status_code=409, detail=f"A valid latest {label} run is required")
    if record_count > MAX_SCENARIO_SERIES:
        raise HTTPException(
            status_code=413,
            detail=f"Decision Lab jobs are limited to {MAX_SCENARIO_SERIES} series; reduce the simulator selection first",
        )
    return payload


def _run_scenario(project_root: Path, request: DecisionLabRunRequest) -> dict[str, Any]:
    try:
        if request.scenario == "purchase_optimization" and request.budget is None:
            raise HTTPException(status_code=422, detail="A scenario budget is required for purchasing optimization")
        _read_bounded_input(
            project_root,
            directory_name="synthetic_inventory",
            pointer_key="records_file",
            records_key="records",
            label="synthetic inventory",
        )
        if request.scenario == "markdown":
            from backend.simulation.run_markdown import run_latest_markdown_scenarios

            report_path = run_latest_markdown_scenarios(project_root)
        elif request.scenario == "purchase_optimization":
            _read_bounded_input(
                project_root,
                directory_name="inventory_risks",
                pointer_key="risk_report",
                records_key="results",
                label="inventory-risk",
            )
            from backend.simulation.run_purchasing import run_latest_purchase_optimization

            report_path = run_latest_purchase_optimization(
                project_root,
                budget=request.budget,
                max_seconds=request.max_seconds,
            )
        else:
            _read_bounded_input(
                project_root,
                directory_name="inventory_risks",
                pointer_key="risk_report",
                records_key="results",
                label="inventory-risk",
            )
            _read_bounded_input(
                project_root,
                directory_name="purchase_optimizations",
                pointer_key="optimization_report",
                records_key="optimization",
                label="purchase-optimization",
            )
            from backend.simulation.run_policy_benchmark import run_latest_policy_benchmark

            report_path = run_latest_policy_benchmark(project_root)
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except HTTPException:
        raise
    except ModuleNotFoundError as error:
        raise HTTPException(status_code=503, detail="The optional purchasing dependency is not installed") from error
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail=f"The {request.scenario} scenario could not be run: {error}") from error
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail="The scenario runner produced an invalid report")
    return report


@router.get("/latest", response_model=DecisionLabLatestResponse)
def get_latest_decision_reports() -> DecisionLabLatestResponse:
    """Return the most recent local scenario reports without exposing filesystem paths."""
    reports: dict[str, DecisionLabRunResponse | None] = {}
    for scenario in _REPORT_SOURCES:
        report = _read_latest_report(PROJECT_ROOT, scenario)
        reports[scenario] = (
            DecisionLabRunResponse(
                scenario=scenario,
                run_id=str(report["run_id"]),
                status=str(report["status"]),
                report=report,
            )
            if report is not None
            else None
        )
    return DecisionLabLatestResponse(reports=reports)


@router.post(
    "/runs",
    response_model=BackgroundJobResponse,
    status_code=202,
    dependencies=[Depends(require_approval_admin)],
)
def queue_decision_scenario(
    request: DecisionLabRunRequest,
    response: Response,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    engine: Engine = Depends(get_database_engine),
) -> BackgroundJobResponse:
    """Queue one local scenario; the dedicated worker performs the calculation."""
    if request.scenario == "purchase_optimization" and request.budget is None:
        raise HTTPException(status_code=422, detail="A scenario budget is required for purchasing optimization")
    try:
        job, created = enqueue_job(
            engine,
            job_type="decision_lab.run",
            payload=request.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key,
        )
    except JobIdempotencyConflict as error:
        raise HTTPException(status_code=409, detail="Idempotency key is already used for different work") from error
    response.status_code = 202 if created else 200
    return BackgroundJobResponse.model_validate(job)
