"""Chronological evaluation for the optional LightGBM candidate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stockwise.forecasting.baselines import build_evaluation_windows, calculate_metrics
from stockwise.forecasting.interval_metrics import evaluate_prequential_intervals
from stockwise.forecasting.lightgbm_model import train_and_forecast


def evaluate_lightgbm(
    series_by_id: Mapping[str, Sequence[float]],
    *,
    initial_window: int = 365,
    horizon: int = 28,
    folds: int = 3,
    max_training_samples_per_series: int | None = None,
    lightgbm_module: Any | None = None,
) -> dict[str, Any]:
    """Evaluate a global LightGBM model on the same chronological windows as baselines."""
    if not series_by_id:
        raise ValueError("at least one series is required")
    series_length = len(next(iter(series_by_id.values())))
    origins, heldout_start = build_evaluation_windows(
        series_length,
        initial_window=initial_window,
        horizon=horizon,
        folds=folds,
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
        model_result = train_and_forecast(
            series_by_id,
            origin,
            horizon,
            max_training_samples_per_series=max_training_samples_per_series,
            lightgbm_module=lightgbm_module,
        )
        actual = [
            float(value)
            for values in series_by_id.values()
            for value in values[origin:origin + horizon]
        ]
        predictions = [
            float(value)
            for series_id in series_by_id
            for value in model_result["predictions"][series_id]
        ]
        rolling_actual.extend(actual)
        rolling_predictions.extend(predictions)
        fold_actual_forecasts.append((actual.copy(), predictions.copy()))
        fold_results.append({
            "fold": fold_number,
            "train_end_index_exclusive": origin,
            "test_start_index": origin,
            "test_end_index_exclusive": origin + horizon,
            "metrics": calculate_metrics(actual, predictions),
        })
        diagnostics.append({
            "fold": fold_number,
            "training_samples": model_result["training_samples"],
            "fit_seconds": model_result["fit_seconds"],
            "prediction_seconds": model_result["prediction_seconds"],
            "clipped_negative_predictions": model_result["clipped_negative_predictions"],
        })

    heldout_result = train_and_forecast(
        series_by_id,
        heldout_start,
        horizon,
        max_training_samples_per_series=max_training_samples_per_series,
        lightgbm_module=lightgbm_module,
    )
    heldout_actual = [
        float(value)
        for values in series_by_id.values()
        for value in values[heldout_start:]
    ]
    heldout_predictions = [
        float(value)
        for series_id in series_by_id
        for value in heldout_result["predictions"][series_id]
    ]
    diagnostics.append({
        "period": "held_out",
        "training_samples": heldout_result["training_samples"],
        "fit_seconds": heldout_result["fit_seconds"],
        "prediction_seconds": heldout_result["prediction_seconds"],
        "clipped_negative_predictions": heldout_result["clipped_negative_predictions"],
    })
    uncertainty_intervals = evaluate_prequential_intervals(
        fold_actual_forecasts, heldout_actual, heldout_predictions, alpha=0.2
    )

    return {
        "model": "lightgbm_global_recursive",
        "series_count": len(series_by_id),
        "observations_per_series": series_length,
        "feature_columns": heldout_result["feature_columns"],
        "parameters": heldout_result["parameters"],
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
