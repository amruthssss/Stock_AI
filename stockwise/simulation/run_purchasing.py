"""Run budget-constrained purchase planning for the latest synthetic inventory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stockwise.simulation.purchasing import solve_purchase_allocation


class PurchasingRunError(ValueError):
    """Raised when purchasing inputs cannot safely be read or published."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PurchasingRunError(f"Could not read {label} at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise PurchasingRunError(f"{label} at {path} must contain a JSON object")
    return payload


def _resolve_report(directory: Path, pointer: dict[str, Any], key: str, label: str) -> Path:
    try:
        report_path = (directory / Path(pointer[key])).resolve()
    except (KeyError, TypeError, ValueError) as error:
        raise PurchasingRunError(f"{label} pointer is missing a valid {key!r}: {error}") from error
    if not report_path.is_relative_to(directory.resolve()):
        raise PurchasingRunError(f"{label} report path escapes its data directory")
    return report_path


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


def run_latest_purchase_optimization(
    project_root: Path,
    *,
    budget: float,
    max_seconds: float = 10.0,
) -> Path:
    """Optimize suggested orders using the latest matching simulation and risk report."""
    project_root = Path(project_root).resolve()
    inventory_directory = project_root / "data" / "processed" / "synthetic_inventory"
    risk_directory = project_root / "data" / "processed" / "inventory_risks"
    inventory_pointer = _read_json(inventory_directory / "latest.json", "synthetic inventory pointer")
    risk_pointer = _read_json(risk_directory / "latest.json", "inventory-risk pointer")

    try:
        simulation_run_id = str(inventory_pointer["run_id"])
        risk_run_id = str(risk_pointer["run_id"])
    except (KeyError, TypeError) as error:
        raise PurchasingRunError(f"Latest pointer is missing a run ID: {error}") from error
    inventory_path = _resolve_report(
        inventory_directory, inventory_pointer, "records_file", "synthetic inventory"
    )
    risk_path = _resolve_report(risk_directory, risk_pointer, "risk_report", "inventory-risk")
    inventory_run = _read_json(inventory_path, "synthetic inventory run")
    risk_run = _read_json(risk_path, "inventory-risk report")

    if inventory_run.get("status") != "completed" or inventory_run.get("run_id") != simulation_run_id:
        raise PurchasingRunError("latest synthetic inventory run is not a matching completed run")
    provenance = inventory_run.get("data_provenance")
    if not isinstance(provenance, dict) or provenance.get("synthetic_operational_records") is not True:
        raise PurchasingRunError("input run is not explicitly marked as synthetic operational data")
    if risk_run.get("status") != "completed" or risk_run.get("run_id") != risk_run_id:
        raise PurchasingRunError("latest inventory-risk report is not a matching completed run")
    if risk_run.get("generated_from_simulation_run_id") != simulation_run_id:
        raise PurchasingRunError("latest risk report was generated from a different inventory run")
    records = inventory_run.get("records")
    risks = risk_run.get("results")
    if not isinstance(records, list) or not records or not isinstance(risks, list) or not risks:
        raise PurchasingRunError("inventory records and risk results must be non-empty lists")

    try:
        optimization = solve_purchase_allocation(
            records,
            risks,
            budget=budget,
            max_seconds=max_seconds,
        )
    except (ValueError, RuntimeError) as error:
        raise PurchasingRunError(f"Could not optimize purchase allocation: {error}") from error

    run_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "status": optimization["status"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_from_simulation_run_id": simulation_run_id,
        "generated_from_risk_run_id": risk_run_id,
        "selection": inventory_run.get("selection", {}),
        "optimization": optimization,
        "limitations": [
            "Inventory, supplier constraints, and unit costs are synthetic assumptions.",
            "Budget is a user-supplied scenario amount; this report does not authorize spending.",
            "Orders are suggestions for offline analysis and are never submitted to a supplier.",
        ],
    }

    output_directory = project_root / "data" / "processed" / "purchase_optimizations"
    run_directory = output_directory / "runs" / run_id
    staging_directory = output_directory / ".staging" / run_id
    try:
        staging_directory.mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_directory / "purchase_optimization.json", payload)
        (output_directory / "runs").mkdir(parents=True, exist_ok=True)
        staging_directory.rename(run_directory)
        _write_json_atomic(output_directory / "latest.json", {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "optimization_report": f"runs/{run_id}/purchase_optimization.json",
        })
    except Exception as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)
        if isinstance(error, PurchasingRunError):
            raise
        raise PurchasingRunError(f"Purchase optimization was not published: {error}") from error
    return run_directory / "purchase_optimization.json"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Create an offline, budget-constrained purchase recommendation report."
    )
    parser.add_argument("--budget", type=float, required=True,
                        help="scenario budget in the same units as the synthetic cost proxy")
    parser.add_argument("--max-seconds", type=float, default=10.0,
                        help="CP-SAT solve time limit; default: 10 seconds")
    args = parser.parse_args()
    try:
        report_path = run_latest_purchase_optimization(
            project_root,
            budget=args.budget,
            max_seconds=args.max_seconds,
        )
    except (OSError, ValueError) as error:
        print(f"Purchase optimization failed: {error}", file=sys.stderr)
        return 1
    print(f"Purchase optimization completed. Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
