"""Chronological evaluation for the optional XGBoost candidate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.forecasting.baselines import build_evaluation_windows, calculate_metrics
from backend.forecasting.interval_metrics import evaluate_prequential_intervals
from backend.forecasting.xgboost_model import train_and_forecast


def evaluate_xgboost(
    series_by_id: Mapping[str, Sequence[float]],
    *,
    initial_window: int = 365,
    horizon: int = 28,
    folds: int = 3,
    max_training_samples_per_series: int | None = None,
    xgboost_module: Any | None = None,
) -> dict[str, Any]:
    """Evaluate XGBoost at the same rolling origins and held-out window as baselines."""
    if not series_by_id:
        raise ValueError("at least one series is required")
    series_length = len(next(iter(series_by_id.values())))
    origins, heldout_start = build_evaluation_windows(
        series_length, initial_window=initial_window, horizon=horizon, folds=folds
    )
    for series_id, values in series_by_id.items():
        if len(values) != series_length:
            raise ValueError(f"series {series_id} has a different number of observations")

    fold_results: list[dict[str, Any]] = []
    rolling_actual: list[float] = []
    rolling_predictions: list[float] = []
    fold_actual_forecasts: list[tuple[list[float], list[float]]] = []
    diagnostics: list[dict[str, Any]] = []

    for fold_number, origin in enumerate(origins, start=1):
        result = train_and_forecast(
            series_by_id,
            origin,
            horizon,
            max_training_samples_per_series=max_training_samples_per_series,
            xgboost_module=xgboost_module,
        )
        actual = [float(value) for values in series_by_id.values()
                  for value in values[origin:origin + horizon]]
        predicted = [float(value) for series_id in series_by_id
                     for value in result["predictions"][series_id]]
        rolling_actual.extend(actual)
        rolling_predictions.extend(predicted)
        fold_actual_forecasts.append((actual.copy(), predicted.copy()))
        fold_results.append({
            "fold": fold_number,
            "train_end_index_exclusive": origin,
            "test_start_index": origin,
            "test_end_index_exclusive": origin + horizon,
            "metrics": calculate_metrics(actual, predicted),
        })
        diagnostics.append({
            "fold": fold_number,
            "training_samples": result["training_samples"],
            "fit_seconds": result["fit_seconds"],
            "prediction_seconds": result["prediction_seconds"],
            "clipped_negative_predictions": result["clipped_negative_predictions"],
        })

    heldout = train_and_forecast(
        series_by_id,
        heldout_start,
        horizon,
        max_training_samples_per_series=max_training_samples_per_series,
        xgboost_module=xgboost_module,
    )
    heldout_actual = [float(value) for values in series_by_id.values()
                      for value in values[heldout_start:]]
    heldout_predictions = [float(value) for series_id in series_by_id
                           for value in heldout["predictions"][series_id]]
    diagnostics.append({
        "period": "held_out",
        "training_samples": heldout["training_samples"],
        "fit_seconds": heldout["fit_seconds"],
        "prediction_seconds": heldout["prediction_seconds"],
        "clipped_negative_predictions": heldout["clipped_negative_predictions"],
    })
    uncertainty_intervals = evaluate_prequential_intervals(
        fold_actual_forecasts, heldout_actual, heldout_predictions, alpha=0.2
    )

    return {
        "model": "xgboost_global_recursive",
        "series_count": len(series_by_id),
        "observations_per_series": series_length,
        "feature_columns": heldout["feature_columns"],
        "parameters": heldout["parameters"],
        "max_training_samples_per_series": max_training_samples_per_series,
        "rolling_origin": {
            "folds": fold_results,
            "metrics": calculate_metrics(rolling_actual, rolling_predictions),
        },
        "held_out": {
            "train_end_index_exclusive": heldout_start,
            "test_start_index": heldout_start,
            "test_end_index_exclusive": series_length,
            "metrics": calculate_metrics(heldout_actual, heldout_predictions),
        },
        "uncertainty_intervals": uncertainty_intervals,
        "diagnostics": diagnostics,
    }
