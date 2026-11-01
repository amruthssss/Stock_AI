"""Validation and transactional import for inventory-risk JSON reports."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from stockwise.persistence.models import InventoryRiskItem, InventoryRiskRun


ITEM_FLOAT_FIELDS = {
    "forecast_average_daily_units": "forecast_average_daily_units",
    "stock_coverage_days": "stock_coverage_days",
    "expected_lead_time_demand_units": "expected_lead_time_demand_units",
    "lead_time_demand_std_units": "lead_time_demand_std_units",
    "lead_time_demand_quantile_units": "lead_time_demand_quantile_units",
    "safety_stock_units": "safety_stock_units",
    "expiry_units_at_risk_fifo": "expiry_units_at_risk",
    "expiry_value_at_risk_proxy": "expiry_value_at_risk_proxy",
}
ITEM_INTEGER_FIELDS = {
    "forecast_days_available": "forecast_days_available",
    "lead_time_days": "lead_time_days",
    "reorder_point_units": "reorder_point_units",
    "incoming_units_within_lead_time": "incoming_units_within_lead_time",
    "available_supply_at_lead_time": "available_supply_at_lead_time",
    "overstock_target_units": "overstock_target_units",
    "overstock_units": "overstock_units",
    "stockout_exposure_units_at_service_level": "stockout_exposure_units",
    "suggested_order_quantity_units": "suggested_order_quantity_units",
}


@dataclass(frozen=True)
class InventoryRiskImport:
    """Validated run metadata and all item rows from one report."""

    run: dict[str, Any]
    items: tuple[dict[str, Any], ...]


def _required_text(container: dict[str, Any], key: str, *, maximum: int | None = None) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required string field '{key}' is missing or invalid")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"Field '{key}' exceeds {maximum} characters")
    return value


def _integer(container: dict[str, Any], key: str, *, minimum: int = 0) -> int:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Field '{key}' must be an integer greater than or equal to {minimum}")
    return value


def _number(container: dict[str, Any], key: str, *, nullable: bool = False) -> float | None:
    if key not in container:
        raise ValueError(f"Required numeric field '{key}' is missing")
    value = container[key]
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Field '{key}' must be a finite number" + (" or null" if nullable else ""))
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Field '{key}' must be finite and non-negative")
    return result


def _iso_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Field '{field_name}' must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Field '{field_name}' must be an ISO date") from error


def parse_inventory_risk_report(
    payload: Any,
    *,
    artifact_path: str,
    artifact_sha256: str,
) -> InventoryRiskImport:
    """Check report provenance and metric invariants before persistence."""
    if not isinstance(payload, dict):
        raise ValueError("Risk report root must be a JSON object")
    if payload.get("status") != "completed":
        raise ValueError("Only completed inventory-risk reports can be imported")
    schema_version = _integer(payload, "schema_version", minimum=1)
    if schema_version not in {1, 2}:
        raise ValueError("Unsupported inventory-risk report schema_version")

    generated_at = _required_text(payload, "generated_at_utc")
    try:
        generated_at_utc = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Field 'generated_at_utc' must be an ISO 8601 timestamp") from error
    if generated_at_utc.tzinfo is None:
        raise ValueError("Field 'generated_at_utc' must include a timezone")
    generated_at_utc = generated_at_utc.astimezone(timezone.utc)

    run_id = _required_text(payload, "run_id", maximum=64)
    simulation_run_id = _required_text(payload, "generated_from_simulation_run_id", maximum=64)
    policy = payload.get("risk_policy")
    selection = payload.get("selection")
    provenance = payload.get("data_provenance")
    results = payload.get("results")
    if not isinstance(policy, dict) or not isinstance(selection, dict) or not isinstance(provenance, dict):
        raise ValueError("Risk report requires risk_policy, selection, and data_provenance objects")
    if not isinstance(results, list) or not results:
        raise ValueError("Risk report must contain a non-empty results array")

    inventory_provenance = _required_text(provenance, "inventory_operational_fields", maximum=100)
    if "synthetic" not in inventory_provenance.lower():
        raise ValueError("Risk report must label operational inventory as synthetic")
    demand_reference = _required_text(provenance, "demand_reference")
    if "observed m5 sales" not in demand_reference.lower():
        raise ValueError("Risk report must identify observed M5 sales as its demand reference")
    if provenance.get("synthetic_inventory_run_id") != simulation_run_id:
        raise ValueError("Risk report simulation provenance does not match its source run ID")

    state_id = _required_text(selection, "state_id", maximum=8)
    category_id = _required_text(selection, "category_id", maximum=32)
    series_count = _integer(selection, "series_count", minimum=1)
    if len(results) != series_count:
        raise ValueError("Risk result count does not match selection.series_count")
    total_matching_series = selection.get("total_matching_series")
    if total_matching_series is not None and (
        isinstance(total_matching_series, bool)
        or not isinstance(total_matching_series, int)
        or total_matching_series < series_count
    ):
        raise ValueError("total_matching_series must be null or at least series_count")
    series_limit = selection.get("series_limit")
    if series_limit is not None and (
        isinstance(series_limit, bool) or not isinstance(series_limit, int) or series_limit < 1
    ):
        raise ValueError("series_limit must be null or a positive integer")
    as_of_date = _iso_date(selection.get("as_of_date"), "selection.as_of_date")

    service_level = _number(policy, "service_level")
    if service_level is None or not 0 < service_level < 1:
        raise ValueError("risk_policy.service_level must be between 0 and 1")
    forecast_model = _required_text(policy, "forecast_model", maximum=64)
    horizon_description = _required_text(policy, "forecast_horizon_days")
    overstock_review_period_days = (
        _integer(policy, "overstock_review_period_days", minimum=1)
        if schema_version >= 2
        else 28
    )
    overstock_calculation = policy.get("overstock_calculation")
    if schema_version >= 2 and (
        not isinstance(overstock_calculation, str)
        or "reorder_point_units" not in overstock_calculation
        or "ceil" not in overstock_calculation
    ):
        raise ValueError("risk_policy.overstock_calculation must document its deterministic rule")

    run = {
        "run_id": run_id,
        "generated_at_utc": generated_at_utc,
        "status": "completed",
        "schema_version": schema_version,
        "simulation_run_id": simulation_run_id,
        "state_id": state_id,
        "category_id": category_id,
        "as_of_date": as_of_date,
        "series_count": series_count,
        "total_matching_series": total_matching_series,
        "series_limit": series_limit,
        "selection_method": _required_text(selection, "selection_method"),
        "service_level": service_level,
        "overstock_review_period_days": overstock_review_period_days,
        "forecast_model": forecast_model,
        "forecast_horizon_description": horizon_description,
        "inventory_provenance": inventory_provenance,
        "demand_reference": demand_reference,
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_sha256,
    }

    items: list[dict[str, Any]] = []
    seen_series: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Each risk result must be a JSON object")
        series_id = _required_text(result, "series_id", maximum=128)
        if series_id in seen_series:
            raise ValueError(f"Duplicate risk result series_id: {series_id}")
        seen_series.add(series_id)
        item_date = _iso_date(result.get("as_of_date"), "result.as_of_date")
        if item_date != as_of_date:
            raise ValueError(f"Risk result date does not match run date for {series_id}")
        item_service_level = _number(result, "service_level")
        if item_service_level is None or not math.isclose(item_service_level, service_level):
            raise ValueError(f"Risk result service level does not match run policy for {series_id}")

        stockout_probability = _number(result, "stockout_probability_normal_approximation")
        if stockout_probability is None or stockout_probability > 1:
            raise ValueError(f"Stockout probability must be between 0 and 1 for {series_id}")
        item = {
            "series_id": series_id,
            "as_of_date": item_date,
            "service_level": item_service_level,
            "stockout_probability": stockout_probability,
        }
        for source_key, target_key in ITEM_FLOAT_FIELDS.items():
            item[target_key] = _number(result, source_key, nullable=(source_key == "stock_coverage_days"))
        for source_key, target_key in ITEM_INTEGER_FIELDS.items():
            if schema_version == 1 and source_key in {"overstock_target_units", "overstock_units"} and source_key not in result:
                # Legacy reports can be imported after deriving the new documented indicator.
                continue
            item[target_key] = _integer(
                result,
                source_key,
                minimum=1 if source_key in {"forecast_days_available", "lead_time_days"} else 0,
            )
        expected_target = item["reorder_point_units"] + math.ceil(
            round(item["forecast_average_daily_units"], 4) * overstock_review_period_days
        )
        if "overstock_target_units" not in item:
            item["overstock_target_units"] = expected_target
        if "overstock_units" not in item:
            item["overstock_units"] = max(0, item["available_supply_at_lead_time"] - expected_target)
        if item["overstock_target_units"] != expected_target:
            raise ValueError(f"Overstock target does not reconcile with policy for {series_id}")
        expected_excess = max(0, item["available_supply_at_lead_time"] - expected_target)
        if item["overstock_units"] != expected_excess:
            raise ValueError(f"Overstock units do not reconcile with target for {series_id}")
        items.append(item)

    if len(seen_series) != series_count:
        raise ValueError("Risk report has duplicate series IDs or a mismatched series count")
    return InventoryRiskImport(run=run, items=tuple(items))


def load_inventory_risk_report(path: Path, *, project_root: Path) -> InventoryRiskImport:
    """Read a risk artifact only from this repository's immutable risk run folders."""
    project_root = Path(project_root).resolve()
    runs_directory = (project_root / "data" / "processed" / "inventory_risks" / "runs").resolve()
    artifact = Path(path).resolve()
    try:
        relative_path = artifact.relative_to(project_root)
        artifact.relative_to(runs_directory)
    except ValueError as error:
        raise ValueError("Risk report must be inside data/processed/inventory_risks/runs") from error
    try:
        content = artifact.read_bytes()
    except OSError as error:
        raise ValueError(f"Could not read risk report at {relative_path.as_posix()}") from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Risk report is not valid UTF-8 JSON") from error
    return parse_inventory_risk_report(
        payload,
        artifact_path=relative_path.as_posix(),
        artifact_sha256=hashlib.sha256(content).hexdigest(),
    )


def import_inventory_risk_report(record: InventoryRiskImport, *, engine: Engine) -> str:
    """Commit one risk run and all item metrics atomically; identical retries are safe."""
    with Session(engine) as session:
        with session.begin():
            existing = session.get(InventoryRiskRun, record.run["run_id"])
            if existing is not None:
                if existing.artifact_sha256 != record.run["artifact_sha256"]:
                    raise ValueError("A different report already uses this inventory-risk run_id")
                return "already_imported"
            run = InventoryRiskRun(**record.run)
            run.items = [InventoryRiskItem(**item) for item in record.items]
            session.add(run)
            session.flush()
    return "imported"
