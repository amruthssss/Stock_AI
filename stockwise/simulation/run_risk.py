"""Run inventory-risk calculations from the latest synthetic M5 simulation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from stockwise.forecasting.baselines import recent_average
from stockwise.simulation.risk import calculate_inventory_risk


class InventoryRiskRunError(ValueError):
    """Raised when risk calculations cannot be safely run or published."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryRiskRunError(f"Could not read {label} at {path}: {error}") from error
    if not isinstance(result, dict):
        raise InventoryRiskRunError(f"{label} at {path} must contain a JSON object")
    return result


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            json.dump(payload, output_file, indent=2, ensure_ascii=False)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_latest_inventory_risks(
    project_root: Path,
    *,
    service_level: float = 0.95,
) -> Path:
    """Calculate and publish risk results for each record in latest simulation run."""
    project_root = Path(project_root).resolve()
    simulation_directory = project_root / "data" / "processed" / "synthetic_inventory"
    pointer = _read_json(simulation_directory / "latest.json", "synthetic inventory pointer")
    try:
        relative_record_path = Path(pointer["records_file"])
        source_path = (simulation_directory / relative_record_path).resolve()
        simulation_run_id = str(pointer["run_id"])
    except (KeyError, TypeError) as error:
        raise InventoryRiskRunError(f"Synthetic inventory pointer is missing required fields: {error}") from error
    if not source_path.is_relative_to(simulation_directory.resolve()):
        raise InventoryRiskRunError("synthetic inventory records path escapes its data directory")

    source = _read_json(source_path, "synthetic inventory run")
    if source.get("status") != "completed" or source.get("run_id") != simulation_run_id:
        raise InventoryRiskRunError("latest synthetic inventory run is not a matching completed run")
    if source.get("data_provenance", {}).get("synthetic_operational_records") is not True:
        raise InventoryRiskRunError("input run is not explicitly marked as synthetic operational data")
    if not isinstance(source.get("records"), list) or not source["records"]:
        raise InventoryRiskRunError("synthetic inventory run contains no records")

    risks: list[dict[str, Any]] = []
    for inventory in source["records"]:
        try:
            recent_sales = inventory["recent_observed_sales"]
            lead_time_days = int(inventory["lead_time_days"])
            as_of_date = date.fromisoformat(str(inventory["as_of_date"]))
            expiry_offsets = [
                (date.fromisoformat(str(batch["expiry_date"])) - as_of_date).days
                for batch in inventory["inventory_batches"]
            ]
            horizon = max([lead_time_days, *expiry_offsets, 28])
            forecast = recent_average(
                recent_sales,
                horizon=horizon,
                window=min(28, len(recent_sales)),
            )
            risks.append(calculate_inventory_risk(
                inventory,
                forecast,
                service_level=service_level,
            ))
        except (KeyError, TypeError, ValueError) as error:
            raise InventoryRiskRunError(
                f"Could not calculate inventory risk for {inventory.get('series_id', '<unknown>')}: {error}"
            ) from error

    run_id = uuid.uuid4().hex
    output_payload = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "completed",
        "generated_from_simulation_run_id": simulation_run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "risk_policy": {
            "service_level": service_level,
            "forecast_model": "recent_average",
            "forecast_horizon_days": "per series; maximum of lead time, batch expiry, and 28 days",
            "overstock_review_period_days": 28,
            "overstock_calculation": "max(available_supply_at_lead_time - (reorder_point_units + ceil(round(average_daily_forecast, 4) * review_period_days)), 0)",
        },
        "data_provenance": {
            "inventory_operational_fields": "synthetic assumptions",
            "demand_reference": "observed M5 sales; may be stockout-censored",
            "synthetic_inventory_run_id": simulation_run_id,
        },
        "selection": source["selection"],
        "results": risks,
        "limitations": [
            "Lead-time probability uses a normal approximation and assumes independent daily demand with stable recent variance.",
            "Overstock is a scenario threshold using a fixed 28-day review period, not a retailer-configured target.",
            "Recent-average forecast and observed M5 sales do not correct for stockout-censored demand.",
            "Expiry risk assumes forecast consumption follows earliest-expiry-first allocation.",
            "Unit-cost exposure is based on a synthetic proxy, not procurement records.",
            "Suggested order quantities obey pack size and MOQ but are not budget-optimized.",
        ],
    }

    output_directory = project_root / "data" / "processed" / "inventory_risks"
    run_directory = output_directory / "runs" / run_id
    staging_directory = output_directory / ".staging" / run_id
    try:
        staging_directory.mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_directory / "risk_report.json", output_payload)
        (output_directory / "runs").mkdir(parents=True, exist_ok=True)
        staging_directory.rename(run_directory)
        _write_json_atomic(output_directory / "latest.json", {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "risk_report": f"runs/{run_id}/risk_report.json",
        })
    except Exception as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)
        if isinstance(error, InventoryRiskRunError):
            raise
        raise InventoryRiskRunError(f"Inventory-risk run was not published: {error}") from error
    return run_directory / "risk_report.json"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Calculate stockout, replenishment, and expiry risk from the latest synthetic inventory run."
    )
    parser.add_argument("--service-level", type=float, default=0.95,
                        help="lead-time demand quantile used for reorder point; default: 0.95")
    args = parser.parse_args()
    try:
        result_path = run_latest_inventory_risks(project_root, service_level=args.service_level)
    except (OSError, ValueError) as error:
        print(f"Inventory-risk calculation failed: {error}", file=sys.stderr)
        return 1
    print(f"Inventory-risk calculation completed. Risk report: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
