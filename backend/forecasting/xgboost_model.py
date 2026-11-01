"""Optional global XGBoost model with recursive daily forecasting."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

import polars as pl

from backend.forecasting.features import FEATURE_COLUMNS, build_training_data, create_lag_features


XGBOOST_PARAMETERS: dict[str, Any] = {
    "objective": "reg:squarederror",
    "eval_metric": "mae",
    "eta": 0.05,
    "max_depth": 5,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 1,
    "tree_method": "hist",
    "seed": 42,
    "nthread": 1,
    "verbosity": 0,
}
XGBOOST_BOOSTING_ROUNDS = 100


class XGBoostUnavailableError(RuntimeError):
    """Raised when XGBoost is selected but its optional package is absent."""


def _xgboost_module() -> Any:
    try:
        import xgboost
    except ImportError as error:
        raise XGBoostUnavailableError(
            "XGBoost is optional and is not installed. Install requirements-forecasting.txt "
            "to enable this candidate."
        ) from error
    return xgboost


def train_and_forecast(
    series_by_id: Mapping[str, Sequence[float]],
    train_end_index_exclusive: int,
    horizon: int,
    *,
    max_training_samples_per_series: int | None = None,
    xgboost_module: Any | None = None,
) -> dict[str, Any]:
    """Fit a global one-step booster and recursively predict the requested horizon."""
    if not series_by_id:
        raise ValueError("at least one series is required")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if xgboost_module is None:
        xgboost_module = _xgboost_module()

    train_features, train_targets = build_training_data(
        series_by_id,
        train_end_index_exclusive,
        max_training_samples_per_series=max_training_samples_per_series,
    )
    fit_started = time.perf_counter()
    training_matrix = xgboost_module.DMatrix(
        train_features.to_numpy(), label=train_targets, feature_names=FEATURE_COLUMNS
    )
    model = xgboost_module.train(
        XGBOOST_PARAMETERS.copy(), training_matrix, num_boost_round=XGBOOST_BOOSTING_ROUNDS
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
        prediction_matrix = xgboost_module.DMatrix(
            feature_frame.to_numpy(), feature_names=FEATURE_COLUMNS
        )
        predicted_values = model.predict(prediction_matrix)
        if len(predicted_values) != len(ordered_ids):
            raise RuntimeError("XGBoost returned the wrong number of predictions")

        for series_id, raw_prediction in zip(ordered_ids, predicted_values, strict=True):
            prediction = float(raw_prediction)
            if not math.isfinite(prediction):
                raise RuntimeError(f"XGBoost returned a non-finite forecast for {series_id}")
            if prediction < 0:
                prediction = 0.0
                clipped_prediction_count += 1
            predictions[series_id].append(prediction)
            contexts[series_id].append(prediction)

    return {
        "predictions": predictions,
        "training_samples": train_features.height,
        "max_training_samples_per_series": max_training_samples_per_series,
        "feature_columns": FEATURE_COLUMNS.copy(),
        "fit_seconds": fit_seconds,
        "prediction_seconds": time.perf_counter() - predict_started,
        "clipped_negative_predictions": clipped_prediction_count,
        "parameters": XGBOOST_PARAMETERS.copy(),
        "boosting_rounds": XGBOOST_BOOSTING_ROUNDS,
    }
