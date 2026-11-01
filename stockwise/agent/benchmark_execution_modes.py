"""Measure sequential and parallel four-role investigations on identical saved evidence."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import statistics
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Self
from urllib.error import HTTPError, URLError

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import SQLAlchemyError

from stockwise.agent.decision_evidence import read_latest_decision_reports
from stockwise.agent.investigation import run_investigation
from stockwise.agent.ollama import _post_json, check_model_available
from stockwise.api.config import PROJECT_ROOT, Settings, get_settings
from stockwise.api.database import create_database_engine


def _p90(values: list[int | float]) -> int | float:
    """Return nearest-rank P90; with five samples this is the largest value."""
    if not values:
        raise ValueError("At least one value is required for a percentile")
    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[index]


class _WindowsResourceSampler:
    """Sample whole-machine CPU and memory with built-in Windows APIs only."""

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("avail_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_extended_virtual", ctypes.c_ulonglong),
        ]

    def __init__(self, interval_seconds: float = 0.25) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[dict[str, float | int | None]] = []
        self._error: str | None = None

    @staticmethod
    def _filetime_value(value: _WindowsResourceSampler._FileTime) -> int:
        return (int(value.high) << 32) | int(value.low)

    def _snapshot(self) -> tuple[int, int, int, int, int]:
        kernel = self._FileTime()
        user = self._FileTime()
        idle = self._FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError("GetSystemTimes failed")
        status = self._MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return (
            self._filetime_value(kernel),
            self._filetime_value(user),
            self._filetime_value(idle),
            int(status.total_phys - status.avail_phys),
            int(status.memory_load),
        )

    def _sample_loop(self) -> None:
        previous: tuple[int, int, int, int, int] | None = None
        while not self._stop.is_set():
            try:
                current = self._snapshot()
                cpu_percent: float | None = None
                if previous is not None:
                    total_delta = (current[0] - previous[0]) + (current[1] - previous[1])
                    idle_delta = current[2] - previous[2]
                    total = total_delta + idle_delta
                    if total > 0:
                        cpu_percent = max(0.0, min(100.0, (total - idle_delta) / total * 100.0))
                self._samples.append(
                    {"cpu_percent": cpu_percent, "memory_used_bytes": current[3], "memory_percent": current[4]}
                )
                previous = current
            except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError) as error:
                self._error = type(error).__name__
                return
            self._stop.wait(self.interval_seconds)

    def __enter__(self) -> Self:
        if os.name != "nt":
            self._error = "windows_system_counters_unavailable"
            return self
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def result(self) -> dict[str, Any]:
        cpu = [sample["cpu_percent"] for sample in self._samples if sample["cpu_percent"] is not None]
        memory_used = [int(sample["memory_used_bytes"]) for sample in self._samples]
        memory_percent = [int(sample["memory_percent"]) for sample in self._samples]
        return {
            "scope": "whole Windows host, including Ollama and other running processes",
            "sample_count": len(self._samples),
            "cpu_avg_percent": round(statistics.mean(cpu), 2) if cpu else None,
            "cpu_peak_percent": round(max(cpu), 2) if cpu else None,
            "memory_peak_used_bytes": max(memory_used) if memory_used else None,
            "memory_avg_percent": round(statistics.mean(memory_percent), 2) if memory_percent else None,
            "unavailable_reason": self._error or ("benchmark_shorter_than_cpu_sample_interval" if not cpu else None),
        }


def _unload_model(settings: Settings) -> tuple[bool, str | None]:
    """Ask local Ollama to release the named model before a cold trial."""
    try:
        response = _post_json(
            settings.ollama_base_url.rstrip("/") + "/api/generate",
            {"Content-Type": "application/json"},
            {"model": settings.ollama_model, "prompt": "", "stream": False, "keep_alive": 0},
            settings.llm_timeout_seconds,
        )
    except (HTTPError, TimeoutError, URLError, OSError, ValueError, TypeError) as error:
        return False, f"unload_request_{type(error).__name__}"
    if not isinstance(response, dict) or response.get("model") != settings.ollama_model:
        return False, "unload_response_unconfirmed"
    return True, None


def summarize_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize measured runs without hiding validation failures or fallback calls."""
    if not trials:
        raise ValueError("At least one benchmark trial is required")

    def distribution(key: str) -> dict[str, int | float | None]:
        values = [trial.get(key) for trial in trials if isinstance(trial.get(key), (int, float))]
        if not values:
            return {"median": None, "p90": None}
        return {"median": statistics.median(values), "p90": _p90(values)}

    statuses = [status for trial in trials for status in trial.get("agent_statuses", {}).values()]
    agent_count = sum(len(trial.get("agent_statuses", {})) for trial in trials)
    completed_count = sum(status == "completed" for status in statuses)
    cold_trials = [trial for trial in trials if trial.get("temperature") == "cold"]
    warm_trials = [trial for trial in trials if trial.get("temperature") == "warm"]

    def temperature_summary(selected: list[dict[str, Any]]) -> dict[str, Any]:
        def summarize(key: str) -> dict[str, int | float | None]:
            values = [trial.get(key) for trial in selected if isinstance(trial.get(key), (int, float))]
            return {
                "median": statistics.median(values) if values else None,
                "p90": _p90(values) if values else None,
            }

        return {
            "trial_count": len(selected),
            "wall_time_ms": summarize("wall_time_ms"),
            "inference_time_ms": summarize("inference_time_ms"),
            "database_retrieval_time_ms": summarize("database_retrieval_time_ms"),
        }

    resources = [trial.get("resources", {}) for trial in trials]

    def resource_summary(key: str) -> dict[str, int | float | None]:
        values = [
            resource.get(key)
            for resource in resources
            if isinstance(resource, dict) and isinstance(resource.get(key), (int, float))
        ]
        return {
            "median": statistics.median(values) if values else None,
            "p90": _p90(values) if values else None,
        }

    return {
        "trial_count": len(trials),
        "median_wall_time_ms": distribution("wall_time_ms")["median"],
        "p90_wall_time_ms": distribution("wall_time_ms")["p90"],
        "median_aggregate_inference_time_ms": distribution("inference_time_ms")["median"],
        "p90_aggregate_inference_time_ms": distribution("inference_time_ms")["p90"],
        "median_database_retrieval_time_ms": distribution("database_retrieval_time_ms")["median"],
        "p90_database_retrieval_time_ms": distribution("database_retrieval_time_ms")["p90"],
        "median_model_calls": distribution("model_calls")["median"],
        "completion_rate": round(completed_count / agent_count, 4) if agent_count else 0.0,
        "all_outputs_valid": all(trial["output_valid"] for trial in trials),
        "all_model_calls_used": all(
            trial["llm_used"] and all(status == "completed" for status in trial["agent_statuses"].values())
            for trial in trials
        ),
        "fallback_rate": round(sum(status != "completed" for status in statuses) / agent_count, 4)
        if agent_count
        else 1.0,
        "cold_trial_count": sum(trial.get("temperature") == "cold" for trial in trials),
        "warm_trial_count": sum(trial.get("temperature") == "warm" for trial in trials),
        "cold_warm_latency": {
            "cold": temperature_summary(cold_trials),
            "warm": temperature_summary(warm_trials),
        },
        "whole_host_resources": {
            "cpu_avg_percent": resource_summary("cpu_avg_percent"),
            "cpu_peak_percent": resource_summary("cpu_peak_percent"),
            "memory_peak_used_bytes": resource_summary("memory_peak_used_bytes"),
            "memory_avg_percent": resource_summary("memory_avg_percent"),
            "samples_by_trial": resources,
        },
        "semantic_accuracy": "not measured; requires a human-reviewed reference set",
    }


def compare_wall_times(modes: dict[str, dict[str, Any]]) -> float | None:
    """Return parallel delta only when both modes used validated model output."""
    sequential = modes.get("sequential", {})
    parallel = modes.get("parallel", {})
    if not all(
        summary.get("all_outputs_valid") is True and summary.get("all_model_calls_used") is True
        for summary in (sequential, parallel)
    ):
        return None
    baseline = sequential.get("median_wall_time_ms")
    candidate = parallel.get("median_wall_time_ms")
    if not isinstance(baseline, (int, float)) or not isinstance(candidate, (int, float)) or baseline <= 0:
        return None
    return round((candidate - baseline) / baseline * 100, 2)


def _run_trial(
    engine: Any,
    *,
    mode: Literal["sequential", "parallel"],
    forecast_run_id: str,
    risk_run_id: str,
    decision_run_id: str | None = None,
    settings: Settings,
    temperature: Literal["cold", "warm"],
) -> dict[str, Any]:
    mode_settings = replace(settings, agent_execution_mode=mode)
    question = (
        "Explain the saved forecast evidence, modeled inventory risks, and the saved markdown decision scenario. "
        "Cite relevant evidence, describe assumptions and limitations, and keep calculated values tied to saved reports."
    )
    with _WindowsResourceSampler() as sampler:
        started = perf_counter()
        result = run_investigation(
            engine,
            checkpointer=InMemorySaver(),
            settings=mode_settings,
            question=question,
            domains=["forecast", "inventory-risk", "decision"],
            run_ids={
                "forecast": forecast_run_id,
                "inventory-risk": risk_run_id,
                "decision": decision_run_id,
            },
            decision_scenarios=["markdown"],
            include_business_intelligence=True,
        )
        wall_time_ms = max(0, int((perf_counter() - started) * 1000))
    resources = sampler.result()
    agent_results = result.get("agent_results", [])
    agent_statuses = {
        item.get("agent", "unknown"): item.get("status", "unknown")
        for item in agent_results
    }
    required_agents = {
        "forecast_intelligence", "inventory_risk", "decision_intelligence", "business_intelligence"
    }
    all_roles_present = set(agent_statuses) == required_agents
    evidence = result.get("evidence", [])
    validation = {
        key: result.get(key, False)
        for key in ("schema_valid", "numeric_claims_valid", "evidence_references_valid", "agent_output_valid")
    }
    output_valid = (
        result.get("status") == "completed"
        and all(validation.get(key) is True for key in (
            "schema_valid", "numeric_claims_valid", "evidence_references_valid", "agent_output_valid"
        ))
        and len(evidence) >= 3
        and all_roles_present
    )
    ollama_metrics = {
        "client_call_duration_ms": [int(item.get("duration_ms", 0)) for item in agent_results if item.get("call_made")],
        "server_total_duration_ns": [item.get("ollama_total_duration_ns") for item in agent_results if item.get("call_made")],
        "model_load_duration_ns": [item.get("ollama_load_duration_ns") for item in agent_results if item.get("call_made")],
        "prompt_eval_duration_ns": [item.get("ollama_prompt_eval_duration_ns") for item in agent_results if item.get("call_made")],
        "eval_duration_ns": [item.get("ollama_eval_duration_ns") for item in agent_results if item.get("call_made")],
        "input_tokens": [item.get("input_tokens") for item in agent_results if item.get("call_made")],
        "output_tokens": [item.get("output_tokens") for item in agent_results if item.get("call_made")],
    }
    database_ms = int(result.get("database_retrieval_duration_ms", 0))
    return {
        "wall_time_ms": wall_time_ms,
        "inference_time_ms": sum(ollama_metrics["client_call_duration_ms"]),
        "database_retrieval_time_ms": database_ms,
        "model_calls": int(result.get("ollama_call_count", 0)),
        "llm_used": bool(result.get("llm_used")),
        "status": result.get("status"),
        "output_valid": output_valid,
        "evidence_reference_integrity": validation.get("evidence_references_valid") is True,
        "numeric_consistency": validation.get("numeric_claims_valid") is True and validation.get("agent_output_valid") is True,
        "agent_statuses": agent_statuses,
        "fallback_reason": result.get("llm_fallback_reason"),
        "temperature": temperature,
        "agent_telemetry": {
            item.get("agent", "unknown"): {
                "status": item.get("status"),
                "client_duration_ms": item.get("duration_ms"),
                "server_total_duration_ns": item.get("ollama_total_duration_ns"),
                "model_load_duration_ns": item.get("ollama_load_duration_ns"),
                "prompt_eval_duration_ns": item.get("ollama_prompt_eval_duration_ns"),
                "eval_duration_ns": item.get("ollama_eval_duration_ns"),
                "input_tokens": item.get("input_tokens"),
                "output_tokens": item.get("output_tokens"),
                "call_made": item.get("call_made"),
            }
            for item in agent_results
        },
        "ollama_metrics": ollama_metrics,
        "resources": resources,
        "gpu_metrics": {"available": False, "reason": "not exposed by the current host environment"},
    }


def run_execution_benchmark(
    engine: Any,
    *,
    forecast_run_id: str,
    risk_run_id: str,
    decision_run_id: str | None = None,
    repeats: int,
    settings: Settings,
) -> dict[str, Any]:
    """Compare sequential and parallel scheduling on the same four-role evidence packet."""
    if repeats < 1 or repeats > 5:
        raise ValueError("repeats must be between 1 and 5")
    if not settings.llm_enabled or not settings.ollama_model:
        raise ValueError("Enable and configure local Ollama before benchmarking agent performance")
    model_available, failure = check_model_available(settings)
    if not model_available:
        raise ValueError(f"Configured local Ollama model is unavailable ({failure or 'unknown failure'})")
    if not decision_run_id:
        raise ValueError("A saved markdown decision run ID is required")

    measurements: dict[str, list[dict[str, Any]]] = {"sequential": [], "parallel": []}
    unload_failures: list[str] = []
    for repeat in range(repeats):
        modes: tuple[Literal["sequential", "parallel"], ...] = (
            ("sequential", "parallel") if repeat % 2 == 0 else ("parallel", "sequential")
        )
        for mode in modes:
            temperature: Literal["cold", "warm"] = "warm"
            if repeat == 0:
                unloaded, reason = _unload_model(settings)
                if not unloaded:
                    unload_failures.append(f"{mode}:{reason}")
                else:
                    temperature = "cold"
            trial = _run_trial(
                engine,
                mode=mode,
                forecast_run_id=forecast_run_id,
                risk_run_id=risk_run_id,
                decision_run_id=decision_run_id,
                settings=settings,
                temperature=temperature,
            )
            trial["trial"] = repeat + 1
            measurements[mode].append(trial)
            print(
                f"{mode} trial {repeat + 1}/{repeats}: {trial['wall_time_ms']} ms, "
                f"{trial['model_calls']} Ollama calls, output valid={trial['output_valid']}, "
                f"temperature={temperature}",
                flush=True,
            )

    modes_report = {
        mode: {"summary": summarize_trials(trials), "trials": trials}
        for mode, trials in measurements.items()
    }
    comparison_valid = all(
        item["summary"]["all_outputs_valid"] and item["summary"]["all_model_calls_used"]
        for item in modes_report.values()
    )
    all_unloads_succeeded = not unload_failures
    return {
        "benchmark_id": uuid.uuid4().hex,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "completed" if comparison_valid else "completed_with_fallback_or_invalid_output",
        "model": settings.ollama_model,
        "execution_modes": {
            "sequential": "control mode: same current agent code, calls specialists in order",
            "parallel": "candidate mode: same code and evidence, independent specialists fan out before BI",
        },
        "scope": "Four local Ollama roles on fixed forecast, inventory-risk, and markdown evidence. Decision files are local JSON; forecast and risk evidence are read from PostgreSQL.",
        "evidence": {
            "forecast_run_id": forecast_run_id,
            "inventory_risk_run_id": risk_run_id,
            "decision_run_id": decision_run_id,
            "decision_scenario": "markdown",
        },
        "repeats_per_mode": repeats,
        "cold_warm_method": "First trial in each mode requests Ollama keep_alive=0 before the graph call; subsequent trials are warm. A failed unload is labelled warm and disclosed.",
        "cold_unload_confirmed_for_all_modes": all_unloads_succeeded,
        "cold_unload_failures": unload_failures,
        "execution_order": "alternates mode order by repetition; each first mode trial is separately unloaded",
        "modes": modes_report,
        "performance_comparison_valid": comparison_valid and all_unloads_succeeded,
        "parallel_wall_time_change_percent": compare_wall_times(
            {mode: details["summary"] for mode, details in modes_report.items()}
        ) if comparison_valid and all_unloads_succeeded else None,
        "before_after_limit": "The checkout has no committed baseline revision; sequential is an execution-mode control, not a recoverable pre-change code snapshot.",
        "quality_limit": "Evidence-reference integrity and numeric consistency are automated checks. Semantic answer accuracy was not measured because no human-reviewed gold-answer set exists.",
        "resource_measurement_limit": "CPU and RAM use whole-host Windows counters and include Ollama and unrelated processes. GPU counters are unavailable.",
        "interpretation_limit": "A small local benchmark is hardware/model specific and does not establish production latency, throughput, or business accuracy.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure sequential and parallel four-role local Ollama investigations on saved evidence."
    )
    parser.add_argument("--forecast-run-id")
    parser.add_argument("--inventory-risk-run-id")
    parser.add_argument("--decision-run-id")
    parser.add_argument("--repeats", type=int, default=5, help="Trials per mode (2 to 5; default: 5)")
    parser.add_argument(
        "--timeout-seconds", type=float, default=120.0,
        help="Per-call local-model timeout in seconds (1 to 300; default: 120)",
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 1 and 300")
    settings = replace(get_settings(args.project_root), llm_timeout_seconds=args.timeout_seconds)
    if not settings.database_url:
        parser.error("STOCKWISE_DATABASE_URL must be configured")
    try:
        engine = create_database_engine(settings.database_url)
    except (SQLAlchemyError, ValueError, TypeError) as error:
        parser.error(f"Database configuration is invalid ({type(error).__name__})")
    try:
        from stockwise.agent.evaluation import _latest_run_ids

        latest_forecast, latest_risk = _latest_run_ids(engine)
        decision_evidence = read_latest_decision_reports(args.project_root, ["markdown"])
        if not decision_evidence:
            raise ValueError("No saved markdown decision report was found")
        report = run_execution_benchmark(
            engine,
            forecast_run_id=args.forecast_run_id or latest_forecast,
            risk_run_id=args.inventory_risk_run_id or latest_risk,
            decision_run_id=args.decision_run_id or decision_evidence[0]["source_run_id"],
            repeats=args.repeats,
            settings=settings,
        )
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    finally:
        engine.dispose()

    output_root = args.project_root / "data" / "processed" / "agent_benchmarks"
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"execution_benchmark_{report['benchmark_id']}.json"
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    for mode, details in report["modes"].items():
        summary = details["summary"]
        print(
            f"{mode}: median/P90 wall {summary['median_wall_time_ms']}/{summary['p90_wall_time_ms']} ms; "
            f"median/P90 model inference {summary['median_aggregate_inference_time_ms']}/"
            f"{summary['p90_aggregate_inference_time_ms']} ms; "
            f"median database retrieval {summary['median_database_retrieval_time_ms']} ms; "
            f"completion rate {summary['completion_rate']}; fallback rate {summary['fallback_rate']}"
        )
    print(
        "Parallel wall-time change: "
        + (f"{report['parallel_wall_time_change_percent']}% (negative is faster)"
           if report["performance_comparison_valid"] else "not reported; validation or cold-unload checks failed")
    )
    print(f"Benchmark report: {output_path}")
    return 0 if report["performance_comparison_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
