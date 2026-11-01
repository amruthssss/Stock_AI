"""Run expiry-aware markdown scenarios for the latest synthetic inventory run."""

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
from stockwise.simulation.markdown import (
    DEFAULT_DISCOUNT_LEVELS,
    DEMAND_LIFT_PER_DISCOUNT,
    MAX_DEMAND_LIFT,
    compare_markdown_scenarios,
)


class MarkdownRunError(ValueError):
    """Raised when markdown scenarios cannot safely be read or published."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarkdownRunError(f"Could not read {label} at {path}: {error}") from error
    if not isinstance(result, dict):
        raise MarkdownRunError(f"{label} at {path} must contain a JSON object")
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


def run_latest_markdown_scenarios(project_root: Path) -> Path:
    """Compare fixed markdown scenarios for each record in the latest simulation."""
    project_root = Path(project_root).resolve()
    simulation_directory = project_root / "data" / "processed" / "synthetic_inventory"
    pointer = _read_json(simulation_directory / "latest.json", "synthetic inventory pointer")
    try:
        source_path = (simulation_directory / Path(pointer["records_file"])).resolve()
        simulation_run_id = str(pointer["run_id"])
    except (KeyError, TypeError) as error:
        raise MarkdownRunError(f"Synthetic inventory pointer is missing required fields: {error}") from error
    if not source_path.is_relative_to(simulation_directory.resolve()):
        raise MarkdownRunError("synthetic inventory records path escapes its data directory")

    source = _read_json(source_path, "synthetic inventory run")
    if source.get("status") != "completed" or source.get("run_id") != simulation_run_id:
        raise MarkdownRunError("latest synthetic inventory run is not a matching completed run")
    provenance = source.get("data_provenance")
    if not isinstance(provenance, dict) or provenance.get("synthetic_operational_records") is not True:
        raise MarkdownRunError("input run is not explicitly marked as synthetic operational data")
    if not isinstance(source.get("records"), list) or not source["records"]:
        raise MarkdownRunError("synthetic inventory run contains no records")

    results: list[dict[str, Any]] = []
    for inventory in source["records"]:
        try:
            recent_sales = inventory["recent_observed_sales"]
            as_of_date = date.fromisoformat(str(inventory["as_of_date"]))
            expiry_offsets = [
                (date.fromisoformat(str(batch["expiry_date"])) - as_of_date).days
                for batch in inventory["inventory_batches"]
            ]
            forecast_horizon = max([28, *expiry_offsets])
            forecast = recent_average(
                recent_sales,
                horizon=forecast_horizon,
                window=min(28, len(recent_sales)),
            )
            results.append(compare_markdown_scenarios(inventory, forecast))
        except (KeyError, TypeError, ValueError) as error:
            raise MarkdownRunError(
                f"Could not evaluate markdown scenarios for {inventory.get('series_id', '<unknown>')}: {error}"
            ) from error

    run_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_from_simulation_run_id": simulation_run_id,
        "scenario_policy": {
            "forecast_model": "recent_average",
            "discount_levels": list(DEFAULT_DISCOUNT_LEVELS),
            "demand_response_formula": "demand_multiplier = min(1 + 0.5 * discount_fraction, 1.5)",
            "demand_lift_per_discount": DEMAND_LIFT_PER_DISCOUNT,
            "maximum_demand_lift": MAX_DEMAND_LIFT,
            "price_changes_applied": False,
        },
        "selection": source["selection"],
        "data_provenance": {
            "inventory_operational_fields": "synthetic assumptions",
            "demand_reference": "observed M5 sales; may be stockout-censored",
            "synthetic_inventory_run_id": simulation_run_id,
        },
        "results": results,
        "limitations": [
            "Markdown demand response is a declared scenario assumption, not an elasticity estimated from M5.",
            "The comparison uses a recent-average point forecast and applies one discount uniformly across on-hand lots.",
            "Incoming purchase orders are excluded because they have no simulated expiry-lot identity.",
            "Unit-cost estimates are synthetic proxies; net value is not retailer profit or a causal impact estimate.",
            "The highest modeled-value scenario is not authorization to change a live price.",
        ],
    }

    output_directory = project_root / "data" / "processed" / "markdown_scenarios"
    run_directory = output_directory / "runs" / run_id
    staging_directory = output_directory / ".staging" / run_id
    try:
        staging_directory.mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_directory / "markdown_report.json", payload)
        (output_directory / "runs").mkdir(parents=True, exist_ok=True)
        staging_directory.rename(run_directory)
        _write_json_atomic(output_directory / "latest.json", {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "markdown_report": f"runs/{run_id}/markdown_report.json",
        })
    except Exception as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)
        if isinstance(error, MarkdownRunError):
            raise
        raise MarkdownRunError(f"Markdown scenario run was not published: {error}") from error
    return run_directory / "markdown_report.json"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Compare hypothetical expiry-aware markdown scenarios for the latest synthetic inventory run."
    )
    parser.parse_args()
    try:
        result_path = run_latest_markdown_scenarios(project_root)
    except (OSError, ValueError) as error:
        print(f"Markdown scenario comparison failed: {error}", file=sys.stderr)
        return 1
    print(f"Markdown scenario comparison completed. Report: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
