"""Read-only access to bounded, completed decision reports for the agent graph."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal


DecisionScenario = Literal["markdown", "purchase_optimization", "policy_benchmark"]

_REPORTS: dict[str, tuple[str, str]] = {
    "markdown": ("markdown_scenarios", "markdown_report"),
    "purchase_optimization": ("purchase_optimizations", "optimization_report"),
    "policy_benchmark": ("policy_benchmarks", "benchmark_report"),
}
_ALLOWED_STATUS = {"completed", "optimal", "feasible", "infeasible", "unknown"}
_MAX_REPORT_BYTES = 1_000_000
_MAX_RESULT_ROWS = 12
_MAX_MARKDOWN_RESULT_ROWS = 6
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SELECTION_KEYS = (
    "series_count", "total_matching_series", "series_limit", "state_id", "category_id",
    "selection_method",
)


class DecisionEvidenceError(ValueError):
    """Raised when a decision report pointer or report is unsafe or invalid."""


def _read_json(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise DecisionEvidenceError("decision report is missing or exceeds its size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DecisionEvidenceError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionEvidenceError("decision report is unreadable") from error
    if not isinstance(payload, dict):
        raise DecisionEvidenceError("decision report must contain a JSON object")
    return payload


def _bounded_report(scenario: str, report: dict[str, Any]) -> dict[str, Any]:
    """Keep relevant saved facts and cap potentially repeated result arrays."""
    selection = report.get("selection")
    if not isinstance(selection, dict):
        raise DecisionEvidenceError("decision report has no valid selection summary")
    series_count = selection.get("series_count")
    if isinstance(series_count, bool) or not isinstance(series_count, int) or series_count < 1:
        raise DecisionEvidenceError("decision report has an invalid selected-series count")
    if scenario == "purchase_optimization" and not isinstance(report.get("optimization"), dict):
        raise DecisionEvidenceError("purchase report has no optimization summary")
    if scenario == "policy_benchmark":
        benchmark = report.get("benchmark")
        if not isinstance(benchmark, dict) or not isinstance(benchmark.get("policies"), list):
            raise DecisionEvidenceError("policy benchmark has no policy comparison rows")
    if scenario == "markdown" and not isinstance(report.get("results"), list):
        raise DecisionEvidenceError("markdown report has no scenario results")
    allowed_keys = {
        "markdown": ("scenario_policy", "selection", "data_provenance", "results", "limitations"),
        "purchase_optimization": ("selection", "optimization", "limitations"),
        "policy_benchmark": ("selection", "benchmark", "limitations"),
    }[scenario]
    bounded_selection = {key: selection[key] for key in _SELECTION_KEYS if key in selection}
    facts: dict[str, Any] = {
        "scenario": scenario,
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "generated_at_utc": report.get("generated_at_utc"),
        "selection": bounded_selection,
    }
    for key in allowed_keys:
        value = report.get(key)
        if key == "selection":
            continue
        if key == "limitations" and isinstance(value, list):
            facts[key] = [item[:300] for item in value[:8] if isinstance(item, str)]
        elif key == "optimization" and isinstance(value, dict):
            keep = (
                "status", "budget", "total_spend", "remaining_budget", "units_covered",
                "candidate_count", "solver", "objective_value", "best_objective_bound",
                "status_notes", "objective_definition",
            )
            facts[key] = {field: value[field] for field in keep if field in value}
        elif key == "benchmark" and isinstance(value, dict):
            policies = value.get("policies")
            bounded_policies = []
            if isinstance(policies, list):
                policy_keys = (
                    "policy_id", "policy_name", "series_count", "total_order_units",
                    "total_exposure_units", "covered_exposure_units", "uncovered_exposure_units",
                    "estimated_order_cost", "exposure_coverage_fraction",
                )
                bounded_policies = [
                    {field: row[field] for field in policy_keys if field in row}
                    for row in policies[:_MAX_RESULT_ROWS]
                    if isinstance(row, dict)
                ]
            facts[key] = {
                field: value[field]
                for field in ("comparison_basis", "optimizer_budget")
                if field in value
            }
            facts[key]["policies"] = bounded_policies
            facts[key]["policies_truncated"] = isinstance(policies, list) and len(policies) > _MAX_RESULT_ROWS
        elif key == "results" and isinstance(value, list):
            result_keys = (
                "series_id", "as_of_date", "observed_retail_price", "synthetic_unit_cost_proxy",
                "demand_response_assumption", "expiry_allocation", "scenarios",
            )
            facts[key] = [
                {field: row[field] for field in result_keys if field in row}
                for row in value[:_MAX_MARKDOWN_RESULT_ROWS]
                if isinstance(row, dict)
            ]
            facts["results_truncated"] = len(value) > _MAX_MARKDOWN_RESULT_ROWS
        else:
            facts[key] = value
    return facts


def read_latest_decision_reports(
    project_root: Path,
    scenarios: list[DecisionScenario] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve known latest pointers or a run ID under an allowlisted scenario directory."""
    if run_id is not None and not _RUN_ID.fullmatch(run_id):
        raise DecisionEvidenceError("decision run identifier is invalid")
    selected = scenarios or list(_REPORTS)
    evidence: list[dict[str, Any]] = []
    for scenario in selected:
        if scenario not in _REPORTS:
            raise DecisionEvidenceError("unsupported decision report type")
        directory_name, pointer_key = _REPORTS[scenario]
        directory = (Path(project_root) / "data" / "processed" / directory_name).resolve()
        if run_id is None:
            pointer_path = directory / "latest.json"
            if not pointer_path.is_file():
                continue
            pointer_path = pointer_path.resolve()
            if not pointer_path.is_relative_to(directory):
                raise DecisionEvidenceError("latest decision pointer escapes its scenario directory")
            pointer = _read_json(pointer_path, max_bytes=32_768)
            relative_path = pointer.get(pointer_key)
            if not isinstance(relative_path, str) or not relative_path:
                raise DecisionEvidenceError("latest decision pointer is missing its report path")
            pointer_run_id = pointer.get("run_id")
            if not isinstance(pointer_run_id, str) or not _RUN_ID.fullmatch(pointer_run_id):
                raise DecisionEvidenceError("latest decision pointer has an invalid run identifier")
            expected_filename = {
                "markdown": "markdown_report.json",
                "purchase_optimization": "purchase_optimization.json",
                "policy_benchmark": "policy_benchmark.json",
            }[scenario]
            path_parts = Path(relative_path).parts
            if path_parts != ("runs", pointer_run_id, expected_filename):
                raise DecisionEvidenceError("latest decision pointer path does not match its run identifier")
        else:
            filename = {
                "markdown": "markdown_report.json",
                "purchase_optimization": "purchase_optimization.json",
                "policy_benchmark": "policy_benchmark.json",
            }[scenario]
            relative_path = f"runs/{run_id}/{filename}"
            pointer_run_id = run_id
            if not (directory / relative_path).is_file():
                continue
        report_path = (directory / relative_path).resolve()
        if not report_path.is_relative_to(directory):
            raise DecisionEvidenceError("latest decision report path escapes its directory")
        report = _read_json(report_path, max_bytes=_MAX_REPORT_BYTES)
        if report.get("run_id") != pointer_run_id or report.get("status") not in _ALLOWED_STATUS:
            raise DecisionEvidenceError("decision report does not match a completed pointer")
        facts = _bounded_report(scenario, report)
        evidence.append({
            "domain": "decision",
            "evidence_id": f"decision_report:{scenario}:{pointer_run_id}",
            "source_table": f"local {scenario} report",
            "source_run_id": pointer_run_id,
            "facts": facts,
        })
    return evidence
