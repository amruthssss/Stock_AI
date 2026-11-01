"""Compare replenishment policies for the latest linked synthetic runs."""

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

from backend.simulation.policy_benchmark import compare_replenishment_policies


class PolicyBenchmarkRunError(ValueError):
    """Raised when policy benchmark inputs cannot safely be read or published."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyBenchmarkRunError(f"Could not read {label} at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise PolicyBenchmarkRunError(f"{label} at {path} must contain a JSON object")
    return payload


def _resolve_report(directory: Path, pointer: dict[str, Any], key: str, label: str) -> Path:
    try:
        path = (directory / Path(pointer[key])).resolve()
    except (KeyError, TypeError, ValueError) as error:
        raise PolicyBenchmarkRunError(f"{label} pointer is missing {key!r}: {error}") from error
    if not path.is_relative_to(directory.resolve()):
        raise PolicyBenchmarkRunError(f"{label} path escapes its data directory")
    return path


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


def run_latest_policy_benchmark(project_root: Path) -> Path:
    """Compare three replenishment policies and publish an immutable report."""
    project_root = Path(project_root).resolve()
    inventory_directory = project_root / "data" / "processed" / "synthetic_inventory"
    risk_directory = project_root / "data" / "processed" / "inventory_risks"
    optimization_directory = project_root / "data" / "processed" / "purchase_optimizations"
    inventory_pointer = _read_json(inventory_directory / "latest.json", "inventory pointer")
    risk_pointer = _read_json(risk_directory / "latest.json", "risk pointer")
    optimization_pointer = _read_json(optimization_directory / "latest.json", "optimization pointer")
    try:
        simulation_run_id = str(inventory_pointer["run_id"])
        risk_run_id = str(risk_pointer["run_id"])
        optimization_run_id = str(optimization_pointer["run_id"])
    except (KeyError, TypeError) as error:
        raise PolicyBenchmarkRunError(f"A latest pointer has no run ID: {error}") from error

    inventory_path = _resolve_report(
        inventory_directory, inventory_pointer, "records_file", "inventory"
    )
    risk_path = _resolve_report(risk_directory, risk_pointer, "risk_report", "risk report")
    optimization_path = _resolve_report(
        optimization_directory, optimization_pointer, "optimization_report", "optimization report"
    )
    inventory_run = _read_json(inventory_path, "inventory run")
    risk_run = _read_json(risk_path, "risk report")
    optimization_run = _read_json(optimization_path, "optimization report")

    if inventory_run.get("status") != "completed" or inventory_run.get("run_id") != simulation_run_id:
        raise PolicyBenchmarkRunError("latest inventory run is not a matching completed run")
    provenance = inventory_run.get("data_provenance")
    if not isinstance(provenance, dict) or provenance.get("synthetic_operational_records") is not True:
        raise PolicyBenchmarkRunError("inventory run is not explicitly marked as synthetic")
    if risk_run.get("status") != "completed" or risk_run.get("run_id") != risk_run_id:
        raise PolicyBenchmarkRunError("latest risk report is not a matching completed run")
    if risk_run.get("generated_from_simulation_run_id") != simulation_run_id:
        raise PolicyBenchmarkRunError("risk report was generated from a different inventory run")
    if optimization_run.get("run_id") != optimization_run_id:
        raise PolicyBenchmarkRunError("latest optimization report does not match its pointer")
    if optimization_run.get("generated_from_simulation_run_id") != simulation_run_id:
        raise PolicyBenchmarkRunError("optimization report was generated from a different inventory run")
    if optimization_run.get("generated_from_risk_run_id") != risk_run_id:
        raise PolicyBenchmarkRunError("optimization report was generated from a different risk report")
    records = inventory_run.get("records")
    risks = risk_run.get("results")
    if not isinstance(records, list) or not records or not isinstance(risks, list) or not risks:
        raise PolicyBenchmarkRunError("inventory records and risk results must be non-empty lists")

    try:
        comparison = compare_replenishment_policies(
            records,
            risks,
            optimization_run,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PolicyBenchmarkRunError(f"Could not compare replenishment policies: {error}") from error

    run_id = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_from_simulation_run_id": simulation_run_id,
        "generated_from_risk_run_id": risk_run_id,
        "generated_from_optimization_run_id": optimization_run_id,
        "selection": inventory_run.get("selection", {}),
        "benchmark": comparison,
    }

    output_directory = project_root / "data" / "processed" / "policy_benchmarks"
    run_directory = output_directory / "runs" / run_id
    staging_directory = output_directory / ".staging" / run_id
    try:
        staging_directory.mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_directory / "policy_benchmark.json", payload)
        (output_directory / "runs").mkdir(parents=True, exist_ok=True)
        staging_directory.rename(run_directory)
        _write_json_atomic(output_directory / "latest.json", {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "benchmark_report": f"runs/{run_id}/policy_benchmark.json",
        })
    except Exception as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)
        if isinstance(error, PolicyBenchmarkRunError):
            raise
        raise PolicyBenchmarkRunError(f"Policy benchmark was not published: {error}") from error
    return run_directory / "policy_benchmark.json"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Compare no-order, risk-reorder, and budget-optimized replenishment policies."
    )
    parser.parse_args()
    try:
        report_path = run_latest_policy_benchmark(project_root)
    except (OSError, ValueError) as error:
        print(f"Policy benchmark failed: {error}", file=sys.stderr)
        return 1
    print(f"Policy benchmark completed. Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
