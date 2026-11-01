"""Optional, bounded LightGBM one-step model with recursive 28-day forecasts."""

from __future__ import annotations

import time
import math
from collections.abc import Mapping, Sequence
from typing import Any

import polars as pl

from backend.forecasting.features import (
    FEATURE_COLUMNS,
    build_training_data,
    create_lag_features,
)


LIGHTGBM_PARAMETERS: dict[str, Any] = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "max_depth": -1,
    "seed": 42,
    "num_threads": 1,
    "verbosity": -1,
    "deterministic": True,
    "force_col_wise": True,
}
LIGHTGBM_BOOSTING_ROUNDS = 100


class LightGBMUnavailableError(RuntimeError):
    """Raised when LightGBM is selected but its optional package is absent."""


def _lightgbm_module() -> Any:
    try:
        import lightgbm
    except ImportError as error:
        raise LightGBMUnavailableError(
            "LightGBM is optional and is not installed. Install requirements-forecasting.txt "
            "to enable this candidate."
        ) from error
    return lightgbm


def train_and_forecast(
    series_by_id: Mapping[str, Sequence[float]],
    train_end_index_exclusive: int,
    horizon: int,
    *,
    max_training_samples_per_series: int | None = None,
    lightgbm_module: Any | None = None,
) -> dict[str, Any]:
    """Fit a global model at a chronological cutoff and forecast recursively.

    The optional lightgbm_module argument allows unit tests to exercise the
    training and prediction flow without installing or loading LightGBM.
    """
    if not series_by_id:
        raise ValueError("at least one series is required")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if lightgbm_module is None:
        lightgbm_module = _lightgbm_module()

    train_features, train_targets = build_training_data(
        series_by_id,
        train_end_index_exclusive,
        max_training_samples_per_series=max_training_samples_per_series,
    )
    fit_started = time.perf_counter()
    training_dataset = lightgbm_module.Dataset(
        train_features,
        label=train_targets,
        feature_name=FEATURE_COLUMNS,
        free_raw_data=False,
    )
    model = lightgbm_module.train(
        LIGHTGBM_PARAMETERS,
        training_dataset,
        num_boost_round=LIGHTGBM_BOOSTING_ROUNDS,
    )
    fit_seconds = time.perf_counter() - fit_started

    contexts = {
        series_id: [float(value) for value in values[:train_end_index_exclusive]]
        for series_id, values in series_by_id.items()
    }
    predictions: dict[str, list[float]] = {series_id: [] for series_id in series_by_id}
    clipped_prediction_count = 0
    predict_started = time.perf_counter()

    for _ in range(horizon):
        ordered_ids = sorted(contexts)
        feature_rows = [
            create_lag_features(contexts[series_id], len(contexts[series_id]))
            for series_id in ordered_ids
        ]
        feature_frame = pl.DataFrame(feature_rows).select(FEATURE_COLUMNS)
        predicted_values = model.predict(feature_frame, num_threads=1)
        if len(predicted_values) != len(ordered_ids):
            raise RuntimeError("LightGBM returned the wrong number of predictions")

        for series_id, raw_prediction in zip(ordered_ids, predicted_values):
            prediction = float(raw_prediction)
            if not math.isfinite(prediction):
                raise RuntimeError(f"LightGBM returned a non-finite forecast for {series_id}")
            if prediction < 0:
                prediction = 0.0
                clipped_prediction_count += 1
            predictions[series_id].append(prediction)
            contexts[series_id].append(prediction)

    prediction_seconds = time.perf_counter() - predict_started
    return {
        "predictions": predictions,
        "training_samples": train_features.height,
        "max_training_samples_per_series": max_training_samples_per_series,
        "feature_columns": FEATURE_COLUMNS.copy(),
        "fit_seconds": fit_seconds,
        "prediction_seconds": prediction_seconds,
        "clipped_negative_predictions": clipped_prediction_count,
        "parameters": LIGHTGBM_PARAMETERS.copy(),
        "boosting_rounds": LIGHTGBM_BOOSTING_ROUNDS,
    }
