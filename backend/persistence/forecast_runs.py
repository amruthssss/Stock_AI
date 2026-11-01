"""Validation and transactional import for completed forecast JSON artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from backend.persistence.models import ForecastMetric, ForecastRun


@dataclass(frozen=True)
class ForecastRunImport:
    """Validated values extracted from one JSON run artifact."""

    run: dict[str, Any]
    metrics: tuple[dict[str, Any], ...]


def _required_string(container: dict[str, Any], key: str, *, maximum: int | None = None) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required string field '{key}' is missing or invalid")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"Field '{key}' exceeds {maximum} characters")
    return value


def _positive_integer(container: dict[str, Any], key: str, *, allow_zero: bool = False) -> int:
    value = container.get(key)
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Field '{key}' must be an integer greater than or equal to {minimum}")
    return value


def _finite_metric(container: dict[str, Any], key: str, *, nullable: bool = False) -> float | None:
    if key not in container:
        raise ValueError(f"Required metric '{key}' is missing")
    value = container.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Metric '{key}' must be a finite number" + (" or null" if nullable else ""))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Metric '{key}' must be finite")
    if key in {"mae", "wape"} and number < 0:
        raise ValueError(f"Metric '{key}' cannot be negative")
    return number


def parse_forecast_run(payload: Any, *, artifact_path: str, artifact_sha256: str) -> ForecastRunImport:
    """Validate required artifact fields and extract aggregate model metrics."""
    if not isinstance(payload, dict):
        raise ValueError("Forecast artifact root must be a JSON object")
    if payload.get("status") != "completed":
        raise ValueError("Only completed forecast runs can be imported")
    schema_version = _positive_integer(payload, "schema_version")
    if schema_version != 1:
        raise ValueError(f"Unsupported forecast artifact schema_version: {schema_version}")

    run_id = _required_string(payload, "run_id", maximum=64)
    generated_at = _required_string(payload, "generated_at_utc")
    try:
        generated_at_utc = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Field 'generated_at_utc' must be an ISO 8601 timestamp") from error
    if generated_at_utc.tzinfo is None:
        raise ValueError("Field 'generated_at_utc' must include a UTC offset")
    generated_at_utc = generated_at_utc.astimezone(timezone.utc)

    provenance = payload.get("data_provenance")
    selection = payload.get("selection")
    evaluation = payload.get("evaluation")
    if not isinstance(provenance, dict) or not isinstance(selection, dict) or not isinstance(evaluation, dict):
        raise ValueError("Forecast artifact requires data_provenance, selection, and evaluation objects")
    if provenance.get("synthetic_data_included") is not False:
        raise ValueError("Forecast run must explicitly identify that it contains no synthetic data")

    run = {
        "run_id": run_id,
        "generated_at_utc": generated_at_utc,
        "status": "completed",
        "schema_version": schema_version,
        "source": _required_string(provenance, "source", maximum=100),
        "m5_ingestion_run_id": _required_string(provenance, "m5_ingestion_run_id", maximum=64),
        "state_id": _required_string(selection, "state_id", maximum=8),
        "category_id": _required_string(selection, "category_id", maximum=32),
        "series_count": _positive_integer(selection, "series_count"),
        "total_matching_series": selection.get("total_matching_series"),
        "series_limit": selection.get("series_limit"),
        "selection_method": _required_string(selection, "selection_method"),
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_sha256,
    }
    total_matching_series = run["total_matching_series"]
    if total_matching_series is not None and (
        isinstance(total_matching_series, bool)
        or not isinstance(total_matching_series, int)
        or total_matching_series < run["series_count"]
    ):
        raise ValueError("Field 'total_matching_series' must be null or at least series_count")
    series_limit = run["series_limit"]
    if series_limit is not None and (
        isinstance(series_limit, bool) or not isinstance(series_limit, int) or series_limit < 1
    ):
        raise ValueError("Field 'series_limit' must be null or a positive integer")

    models_by_period: dict[str, dict[str, Any]] = {}
    for period in ("rolling_origin", "held_out"):
        period_result = evaluation.get(period)
        if not isinstance(period_result, dict):
            raise ValueError(f"Evaluation is missing the '{period}' object")
        model_results = period_result.get("models")
        if not isinstance(model_results, dict) or not model_results:
            raise ValueError(f"Evaluation period '{period}' must contain a non-empty models object")
        models_by_period[period] = dict(model_results)

    optional_models = payload.get("optional_models", {})
    if not isinstance(optional_models, dict):
        raise ValueError("Field 'optional_models' must be a JSON object")
    for optional_name, optional_result in optional_models.items():
        if not isinstance(optional_result, dict):
            raise ValueError(f"Optional model '{optional_name}' must be a JSON object")
        optional_status = optional_result.get("status")
        if optional_status in {"failed", "skipped"}:
            # The source JSON remains the full record of unavailable optional models.
            continue
        if optional_status != "completed":
            raise ValueError(f"Optional model '{optional_name}' has an unsupported status")
        optional_evaluation = optional_result.get("evaluation")
        if not isinstance(optional_evaluation, dict):
            raise ValueError(f"Completed optional model '{optional_name}' is missing evaluation data")
        model_name = optional_evaluation.get("model", optional_name)
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 64:
            raise ValueError(f"Optional model '{optional_name}' has an invalid model name")
        for period, optional_period in (
            ("rolling_origin", optional_evaluation.get("rolling_origin")),
            ("held_out", optional_evaluation.get("held_out")),
        ):
            if not isinstance(optional_period, dict) or not isinstance(optional_period.get("metrics"), dict):
                raise ValueError(f"Optional model '{optional_name}' is missing {period} metrics")
            if model_name in models_by_period[period]:
                raise ValueError(f"Optional model '{model_name}' duplicates an evaluation model")
            models_by_period[period][model_name] = optional_period["metrics"]

    metrics: list[dict[str, Any]] = []
    observed_model_names: dict[str, set[str]] = {}
    for period, model_results in models_by_period.items():
        names: set[str] = set()
        for model_name, model_metrics in model_results.items():
            if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 64:
                raise ValueError(f"Evaluation period '{period}' contains an invalid model name")
            if not isinstance(model_metrics, dict):
                raise ValueError(f"Metrics for model '{model_name}' must be a JSON object")
            names.add(model_name)
            metrics.append(
                {
                    "evaluation_period": period,
                    "model_name": model_name,
                    "mae": _finite_metric(model_metrics, "mae"),
                    "wape": _finite_metric(model_metrics, "wape", nullable=True),
                    "bias": _finite_metric(model_metrics, "bias", nullable=True),
                    "observations": _positive_integer(model_metrics, "observations"),
                }
            )
        observed_model_names[period] = names
    if observed_model_names["rolling_origin"] != observed_model_names["held_out"]:
        raise ValueError("Rolling-origin and held-out periods must contain the same model names")

    return ForecastRunImport(run=run, metrics=tuple(metrics))


def load_forecast_run(path: Path, *, project_root: Path) -> ForecastRunImport:
    """Read a run artifact only from the repository's processed forecast folder."""
    project_root = Path(project_root).resolve()
    forecast_directory = (project_root / "data" / "processed" / "forecasts").resolve()
    artifact = Path(path).resolve()
    try:
        relative_path = artifact.relative_to(project_root)
        artifact.relative_to(forecast_directory)
    except ValueError as error:
        raise ValueError("Forecast artifact must be inside data/processed/forecasts") from error
    try:
        content = artifact.read_bytes()
    except OSError as error:
        raise ValueError(f"Could not read forecast artifact at {relative_path.as_posix()}") from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Forecast artifact is not valid UTF-8 JSON") from error
    return parse_forecast_run(
        payload,
        artifact_path=relative_path.as_posix(),
        artifact_sha256=hashlib.sha256(content).hexdigest(),
    )


def import_forecast_run(record: ForecastRunImport, *, engine: Engine) -> str:
    """Commit a run and all aggregate metrics atomically; identical imports are no-ops."""
    with Session(engine) as session:
        with session.begin():
            existing = session.get(ForecastRun, record.run["run_id"])
            if existing is not None:
                if existing.artifact_sha256 != record.run["artifact_sha256"]:
                    raise ValueError("A different artifact already uses this forecast run_id")
                return "already_imported"

            run = ForecastRun(**record.run)
            run.metrics = [ForecastMetric(**metric) for metric in record.metrics]
            session.add(run)
            session.flush()
    return "imported"
