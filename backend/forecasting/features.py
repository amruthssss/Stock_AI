"""Leakage-safe lag and calendar features for one-step global forecasts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import polars as pl


LOOKBACK_DAYS = 28
FEATURE_COLUMNS = [
    "lag_1",
    "lag_7",
    "lag_14",
    "lag_28",
    "mean_7",
    "mean_28",
    "weekday_sin",
    "weekday_cos",
]


def create_lag_features(history: Sequence[float], target_day_index: int) -> dict[str, float]:
    """Create features for the next day using only the values in history.

    `target_day_index` is zero-based and must equal len(history), preventing a
    caller from supplying a day index that points inside the observed future.
    """
    if target_day_index != len(history):
        raise ValueError("target_day_index must equal the number of past observations")
    if len(history) < LOOKBACK_DAYS:
        raise ValueError(f"at least {LOOKBACK_DAYS} past observations are required")
    for value in history:
        if value < 0 or not math.isfinite(float(value)):
            raise ValueError("demand history must be finite and non-negative")

    return _create_lag_features_from_valid_history(history, target_day_index)


def _create_lag_features_from_valid_history(
    history: Sequence[float], target_day_index: int
) -> dict[str, float]:
    """Create features after the caller has checked the full history once."""

    weekday_angle = 2 * math.pi * (target_day_index % 7) / 7
    return {
        "lag_1": float(history[-1]),
        "lag_7": float(history[-7]),
        "lag_14": float(history[-14]),
        "lag_28": float(history[-28]),
        "mean_7": sum(float(value) for value in history[-7:]) / 7,
        "mean_28": sum(float(value) for value in history[-28:]) / 28,
        "weekday_sin": math.sin(weekday_angle),
        "weekday_cos": math.cos(weekday_angle),
    }


def build_training_data(
    series_by_id: Mapping[str, Sequence[float]],
    train_end_index_exclusive: int,
    *,
    max_training_samples_per_series: int | None = None,
) -> tuple[pl.DataFrame, list[float]]:
    """Build one-step samples before the cutoff, optionally spread across time.

    The optional cap bounds memory for a global model across thousands of M5
    series. Chosen days are evenly spaced, deterministic, and include the last
    available training target. Evaluation still covers every selected series.
    """
    if not series_by_id:
        raise ValueError("at least one series is required")
    if train_end_index_exclusive <= LOOKBACK_DAYS:
        raise ValueError("training cutoff must include days after the 28-day lookback")

    expected_length = len(next(iter(series_by_id.values())))
    if train_end_index_exclusive > expected_length:
        raise ValueError("training cutoff exceeds the available series length")
    if max_training_samples_per_series is not None and max_training_samples_per_series < 1:
        raise ValueError("max_training_samples_per_series must be at least 1")

    feature_rows: list[dict[str, float]] = []
    targets: list[float] = []
    for series_id in sorted(series_by_id):
        values = series_by_id[series_id]
        if len(values) != expected_length:
            raise ValueError(f"series {series_id} has a different number of days")
        if train_end_index_exclusive > len(values):
            raise ValueError(f"training cutoff exceeds the length of series {series_id}")

        # Validate the observed training prefix once. Calling the public
        # create_lag_features for every target would re-scan each history and
        # make broad M5 training roughly proportional to series_count * days^2.
        training_values = values[:train_end_index_exclusive]
        for day_index, value in enumerate(training_values):
            if value < 0 or not math.isfinite(float(value)):
                raise ValueError(
                    f"series {series_id} has invalid demand at index {day_index}; "
                    "values must be finite and non-negative"
                )

        target_indices = _training_target_indices(
            train_end_index_exclusive,
            max_training_samples_per_series,
        )
        for target_index in target_indices:
            history = training_values[:target_index]
            feature_rows.append(
                _create_lag_features_from_valid_history(history, target_index)
            )
            target = float(values[target_index])
            targets.append(target)

    if not feature_rows:
        raise ValueError("no training rows were produced")
    return pl.DataFrame(feature_rows).select(FEATURE_COLUMNS), targets


def _training_target_indices(
    train_end_index_exclusive: int,
    max_training_samples_per_series: int | None,
) -> range | list[int]:
    start = LOOKBACK_DAYS
    count = train_end_index_exclusive - start
    if max_training_samples_per_series is None or count <= max_training_samples_per_series:
        return range(start, train_end_index_exclusive)
    if max_training_samples_per_series == 1:
        return [train_end_index_exclusive - 1]
    last = train_end_index_exclusive - 1
    return [
        start + round(index * (last - start) / (max_training_samples_per_series - 1))
        for index in range(max_training_samples_per_series)
    ]
