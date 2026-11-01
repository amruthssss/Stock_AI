"""Benchmark the configured local Ollama model without running StockWise workflows."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from time import perf_counter
from typing import Any

from backend.agent.contracts import (
    SpecialistResponse,
    validate_specialist_response,
)
from backend.agent.ollama import check_model_available, generate_structured_response
from backend.api.config import Settings

_BENCHMARK_EVIDENCE_ID = "Evidence-1"
_BENCHMARK_SYSTEM_PROMPT = (
    "Return a short qualitative summary of the supplied example evidence. Cite its evidence ID. "
    "Do not include numbers or make claims beyond the supplied evidence."
)
_BENCHMARK_PAYLOAD: dict[str, Any] = {
    "question": "Summarize the example evidence qualitatively.",
    "evidence": [
        {
            "evidence_id": _BENCHMARK_EVIDENCE_ID,
            "domain": "forecast",
            "facts": {"description": "A synthetic example used only to check local model latency."},
        }
    ],
    "required_evidence_ids": [_BENCHMARK_EVIDENCE_ID],
    "output_requirements": {
        "qualitative_only": True,
        "cite_evidence_ids": True,
        "no_numeric_claims": True,
    },
}


def _docker_stats(container_name: str | None) -> dict[str, str] | None:
    """Read an optional, one-shot Docker CPU and memory snapshot without changing state."""
    if not container_name:
        return None
    try:
        completed = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}|{{.MemUsage}}",
                container_name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}
    if completed.returncode != 0:
        return {"status": "unavailable"}
    values = completed.stdout.strip().split("|", maxsplit=1)
    if len(values) != 2:
        return {"status": "unavailable"}
    return {"cpu": values[0].strip(), "memory": values[1].strip()}


def _duration_ms(value_ns: int | None) -> float | None:
    if value_ns is None:
        return None
    return round(value_ns / 1_000_000, 3)


def _sample(label: str, settings: Settings, container_name: str | None) -> dict[str, Any]:
    """Make one direct model call and report timings and validation, never response text."""
    docker_before = _docker_stats(container_name)
    started = perf_counter()
    result = generate_structured_response(
        settings,
        system_prompt=_BENCHMARK_SYSTEM_PROMPT,
        user_payload=_BENCHMARK_PAYLOAD,
        response_model=SpecialistResponse,
        max_output_tokens=160,
    )
    wall_ms = round((perf_counter() - started) * 1000, 3)
    references_valid = False
    numeric_valid = False
    if isinstance(result.value, SpecialistResponse):
        references_valid, numeric_valid = validate_specialist_response(
            result.value,
            allowed_evidence_ids={_BENCHMARK_EVIDENCE_ID},
        )
    failure_reason = result.failure_reason
    if result.value is not None and not (references_valid and numeric_valid):
        failure_reason = "benchmark_response_validation_failed"
    return {
        "sample": label,
        "status": "valid" if result.value is not None and references_valid and numeric_valid else "failed",
        "failure_reason": failure_reason,
        "wall_ms": wall_ms,
        "ollama_total_ms": _duration_ms(result.total_duration_ns),
        "ollama_load_ms": _duration_ms(result.load_duration_ns),
        "ollama_prompt_eval_ms": _duration_ms(result.prompt_eval_duration_ns),
        "ollama_generation_ms": _duration_ms(result.eval_duration_ns),
        "prompt_tokens": result.input_tokens,
        "generated_tokens": result.output_tokens,
        "references_valid": references_valid,
        "numeric_claims_valid": numeric_valid,
        "docker_before": docker_before,
        "docker_after": _docker_stats(container_name),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure direct Ollama cold/first and warm chat latency. This does not run the "
            "investigation graph, query PostgreSQL, save results, or send notifications."
        )
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:11434",
        help="Local Ollama address reachable from this Python process.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("STOCKWISE_OLLAMA_MODEL", "qwen3.5:4b"),
        help="Exact installed Ollama model name.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Benchmark-only HTTP timeout; does not change the application setting.",
    )
    parser.add_argument(
        "--warm-samples",
        type=int,
        default=3,
        help="Number of repeated warm calls after the first call.",
    )
    parser.add_argument(
        "--container",
        default="stockwise-ollama-1",
        help="Docker container name for optional CPU/memory snapshots; use an empty value to skip.",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 1 and 600")
    if not 1 <= args.warm_samples <= 10:
        parser.error("--warm-samples must be between 1 and 10")
    if not args.model.strip():
        parser.error("--model must not be empty")
    args.container = args.container.strip() or None
    return args


def main() -> int:
    args = _parse_args()
    settings = Settings(
        database_url=None,
        llm_enabled=True,
        ollama_base_url=args.base_url,
        ollama_model=args.model.strip(),
        llm_timeout_seconds=args.timeout_seconds,
    )

    preflight_started = perf_counter()
    model_available, failure_reason = check_model_available(settings)
    preflight_ms = round((perf_counter() - preflight_started) * 1000, 3)
    output: dict[str, Any] = {
        "benchmark": "direct_ollama_structured_chat",
        "model": settings.ollama_model,
        "base_url": settings.ollama_base_url,
        "timeout_seconds": settings.llm_timeout_seconds,
        "preflight": {
            "status": "available" if model_available else "failed",
            "duration_ms": preflight_ms,
            "failure_reason": failure_reason,
        },
        "samples": [],
        "limitations": [
            "The first sample is only a cold-load sample if Ollama had unloaded the model beforehand.",
            "Docker CPU and memory readings are one-shot snapshots, not continuous peak measurements.",
            "This isolated synthetic request does not measure full graph, database, or API latency.",
        ],
    }
    if not model_available:
        print(json.dumps(output, indent=2))
        return 1

    samples = [_sample("first_call", settings, args.container)]
    for index in range(args.warm_samples):
        samples.append(_sample(f"warm_{index + 1}", settings, args.container))
    output["samples"] = samples
    output["overall_status"] = (
        "passed" if all(sample["status"] == "valid" for sample in samples) else "failed"
    )
    print(json.dumps(output, indent=2))
    return 0 if output["overall_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
