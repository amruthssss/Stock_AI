"""Optional local MLflow tracking for completed forecast evaluation JSON runs."""

from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path
from typing import Any


EXPERIMENT_NAME = "StockWise M5 Forecast Evaluation"


class MLflowUnavailableError(RuntimeError):
    """Raised when MLflow tracking is requested without the optional package."""


def _mlflow_module() -> Any:
    try:
        import mlflow
    except ImportError as error:
        raise MLflowUnavailableError(
            "MLflow is optional and is not installed. Install requirements-tracking.txt "
            "to enable experiment tracking."
        ) from error
    return mlflow


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _log_numeric_metrics(mlflow: Any, prefix: str, metrics: Any) -> None:
    if not isinstance(metrics, dict):
        return
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            metric_key = f"{prefix.replace('/', '__')}__{name}"
            mlflow.log_metric(metric_key, number)


def _log_interval_metrics(mlflow: Any, model_name: str, intervals: Any) -> None:
    if not isinstance(intervals, dict):
        return
    for period in ("rolling_origin", "held_out"):
        period_result = intervals.get(period)
        if isinstance(period_result, dict):
            _log_numeric_metrics(
                mlflow, f"interval/{period}/{model_name}", period_result.get("metrics")
            )


def _forecast_parameters(report: dict[str, Any]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    selection = report.get("selection", {})
    for name in (
        "state_id", "category_id", "series_count", "total_matching_series", "series_limit"
    ):
        value = selection.get(name)
        if value is not None:
            parameters[f"selection_{name}"] = str(value)
    parameters["selection_method"] = str(selection.get("selection_method", "unknown"))[:6000]

    evaluation = report.get("evaluation", {})
    for name, value in evaluation.get("parameters", {}).items():
        if value is not None:
            parameters[f"backtest_{name}"] = str(value)
    for model_name, model in report.get("optional_models", {}).items():
        result = model.get("evaluation", {}) if isinstance(model, dict) else {}
        for name, value in result.get("parameters", {}).items():
            if value is not None and isinstance(value, (str, int, float, bool)):
                parameters[f"{model_name}_{name}"] = str(value)
    return parameters


def _log_report_metrics(mlflow: Any, report: dict[str, Any]) -> None:
    evaluation = report.get("evaluation", {})
    for period in ("rolling_origin", "held_out"):
        period_result = evaluation.get(period, {})
        for model_name, metrics in period_result.get("models", {}).items():
            _log_numeric_metrics(mlflow, f"point/{period}/{model_name}", metrics)

    for model_name, intervals in evaluation.get("uncertainty_intervals", {}).items():
        _log_interval_metrics(mlflow, model_name, intervals)

    for candidate_name, candidate in report.get("optional_models", {}).items():
        if not isinstance(candidate, dict):
            continue
        mlflow.set_tag(f"optional_model_{candidate_name}_status", str(candidate.get("status")))
        if candidate.get("status") != "completed":
            continue
        result = candidate.get("evaluation", {})
        model_name = result.get("model", candidate_name)
        for period in ("rolling_origin", "held_out"):
            period_result = result.get(period, {})
            _log_numeric_metrics(
                mlflow,
                f"point/{period}/{model_name}",
                period_result.get("metrics"),
            )
        _log_interval_metrics(mlflow, model_name, result.get("uncertainty_intervals"))


def track_forecast_run(
    report: dict[str, Any],
    report_path: Path,
    tracking_directory: Path,
    *,
    mlflow_module: Any | None = None,
) -> dict[str, str]:
    """Log parameters, point/interval metrics, and JSON to a local MLflow file store."""
    # MLflow 3.16 requires an explicit opt-in for its legacy local file store.
    # This lightweight client is selected to preserve StockWise's PyArrow 26 pin.
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    if mlflow_module is None:
        mlflow_module = _mlflow_module()
    report_path = Path(report_path).resolve()
    tracking_directory = Path(tracking_directory).resolve()
    tracking_directory.mkdir(parents=True, exist_ok=True)
    artifact_directory = tracking_directory / "artifacts"
    artifact_directory.mkdir(exist_ok=True)
    store_directory = tracking_directory / "store"
    store_directory.mkdir(exist_ok=True)
    tracking_uri = store_directory.as_uri()

    mlflow_module.set_tracking_uri(tracking_uri)
    client = mlflow_module.tracking.MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(EXPERIMENT_NAME)
    if experiment is None:
        experiment_id = client.create_experiment(
            EXPERIMENT_NAME, artifact_location=artifact_directory.as_uri()
        )
    else:
        experiment_id = experiment.experiment_id

    tags = {
        "stockwise_run_id": str(report.get("run_id", "unknown")),
        "data_source": str(report.get("data_provenance", {}).get("source", "unknown"))[:250],
        "m5_ingestion_run_id": str(
            report.get("data_provenance", {}).get("m5_ingestion_run_id", "unknown")
        )[:250],
    }
    with mlflow_module.start_run(
        experiment_id=experiment_id,
        run_name=str(report.get("run_id", "stockwise-forecast")),
        tags=tags,
    ) as active_run:
        run_id = active_run.info.run_id
        metadata = {
            "status": "completed",
            "experiment_name": EXPERIMENT_NAME,
            "experiment_id": str(experiment_id),
            "mlflow_run_id": run_id,
        "tracking_uri": tracking_uri,
        }
        report["mlflow_tracking"] = metadata
        _write_json_atomic(report_path, report)
        mlflow_module.log_params(_forecast_parameters(report))
        _log_report_metrics(mlflow_module, report)
        mlflow_module.log_artifact(str(report_path), artifact_path="forecast_json")
    return metadata
