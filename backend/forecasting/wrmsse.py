"""M5's 12-level Weighted Root Mean Squared Scaled Error metric."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np


M5_HIERARCHY_LEVELS: dict[str, tuple[str, ...]] = {
    "total": (),
    "state": ("state_id",),
    "store": ("store_id",),
    "category": ("cat_id",),
    "department": ("dept_id",),
    "state_category": ("state_id", "cat_id"),
    "state_department": ("state_id", "dept_id"),
    "store_category": ("store_id", "cat_id"),
    "store_department": ("store_id", "dept_id"),
    "item": ("item_id",),
    "item_state": ("item_id", "state_id"),
    "item_store": ("item_id", "store_id"),
}


def calculate_m5_wrmsse(
    history_by_id: Mapping[str, Sequence[float]],
    actual_by_id: Mapping[str, Sequence[float]],
    forecast_by_id: Mapping[str, Sequence[float]],
    metadata_by_id: Mapping[str, Mapping[str, str]],
    revenue_by_id: Mapping[str, float],
) -> dict[str, Any]:
    """Calculate WRMSSE for a complete set of M5 bottom-level series.

    `revenue_by_id` must contain each bottom series' dollars sold in the 28-day
    training window immediately before the evaluated forecast origin. The
    caller is responsible for building it from unit sales and M5 sell prices.
    The scorer requires all bottom series represented in its input; using a
    filtered category subset does not produce the official full-M5 metric.
    """
    ids = sorted(history_by_id)
    if not ids:
        raise ValueError("at least one M5 bottom-level series is required")
    expected_ids = set(ids)
    for name, values in (
        ("actual", actual_by_id),
        ("forecast", forecast_by_id),
        ("metadata", metadata_by_id),
        ("revenue weights", revenue_by_id),
    ):
        if set(values) != expected_ids:
            raise ValueError(f"{name} series IDs must exactly match the training histories")

    return calculate_m5_wrmsse_arrays(
        ids,
        np.asarray([history_by_id[series_id] for series_id in ids], dtype=np.float64),
        np.asarray([actual_by_id[series_id] for series_id in ids], dtype=np.float64),
        np.asarray([forecast_by_id[series_id] for series_id in ids], dtype=np.float64),
        {series_id: metadata_by_id[series_id] for series_id in ids},
        np.asarray([revenue_by_id[series_id] for series_id in ids], dtype=np.float64),
    )


def calculate_m5_wrmsse_arrays(
    series_ids: Sequence[str],
    histories: np.ndarray,
    actuals: np.ndarray,
    forecasts: np.ndarray,
    metadata_by_id: Mapping[str, Mapping[str, str]],
    revenues: np.ndarray,
) -> dict[str, Any]:
    """Array-oriented scorer for complete M5 inputs without per-series copies."""
    ids = list(series_ids)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("M5 bottom-level series IDs must be non-empty and unique")
    if set(metadata_by_id) != set(ids):
        raise ValueError("metadata series IDs must exactly match the bottom-level IDs")
    histories = np.asarray(histories, dtype=np.float64)
    actuals = np.asarray(actuals, dtype=np.float64)
    forecasts = np.asarray(forecasts, dtype=np.float64)
    revenues = np.asarray(revenues, dtype=np.float64)
    if histories.ndim != 2 or histories.shape[0] != len(ids):
        raise ValueError("history matrix must have one row per bottom-level series")
    if actuals.ndim != 2 or forecasts.shape != actuals.shape or actuals.shape[0] != len(ids):
        raise ValueError("actual and forecast matrices must match and have one row per series")
    if revenues.shape != (len(ids),):
        raise ValueError("revenue weights must have one value per bottom-level series")

    training_days = histories.shape[1]
    horizon = actuals.shape[1]
    if training_days < 2 or horizon < 1:
        raise ValueError("WRMSSE needs at least two training days and one forecast day")

    fields = ("item_id", "dept_id", "cat_id", "store_id", "state_id")
    metadata_values: dict[str, list[str]] = {field: [] for field in fields}

    for row_number, series_id in enumerate(ids):
        history = histories[row_number]
        actual = actuals[row_number]
        forecast = forecasts[row_number]
        if not np.isfinite(history).all() or (history < 0).any():
            raise ValueError(f"series {series_id} has invalid training sales")
        if not np.isfinite(actual).all() or (actual < 0).any():
            raise ValueError(f"series {series_id} has invalid actual sales")
        if not np.isfinite(forecast).all() or (forecast < 0).any():
            raise ValueError(f"series {series_id} has invalid forecast sales")
        revenue = float(revenues[row_number])
        if not math.isfinite(revenue) or revenue < 0:
            raise ValueError(f"series {series_id} has an invalid revenue weight")
        row_metadata = metadata_by_id[series_id]
        for field in fields:
            value = row_metadata.get(field)
            if not value:
                raise ValueError(f"series {series_id} has no {field} metadata")
            metadata_values[field].append(str(value))

    total_revenue = float(revenues.sum())
    if total_revenue <= 0:
        raise ValueError("28-day training-window revenue must be positive")
    revenues /= total_revenue

    level_results: dict[str, dict[str, Any]] = {}
    for level_name, group_fields in M5_HIERARCHY_LEVELS.items():
        group_index, group_count = _group_indices(ids, metadata_values, group_fields)
        aggregate_history = np.zeros((group_count, training_days), dtype=np.float64)
        for day_index in range(training_days):
            aggregate_history[:, day_index] = np.bincount(
                group_index,
                weights=histories[:, day_index],
                minlength=group_count,
            )
        aggregate_actual = np.zeros((group_count, horizon), dtype=np.float64)
        aggregate_forecast = np.zeros((group_count, horizon), dtype=np.float64)
        for day_index in range(horizon):
            aggregate_actual[:, day_index] = np.bincount(
                group_index,
                weights=actuals[:, day_index],
                minlength=group_count,
            )
            aggregate_forecast[:, day_index] = np.bincount(
                group_index,
                weights=forecasts[:, day_index],
                minlength=group_count,
            )
        node_revenue = np.bincount(group_index, weights=revenues, minlength=group_count)
        scales = _m5_scales(aggregate_history, node_revenue)
        errors = aggregate_actual - aggregate_forecast
        rmsse = np.sqrt(np.mean(np.square(errors), axis=1) / scales)
        level_score = float(np.dot(node_revenue, rmsse))
        level_results[level_name] = {
            "weighted_rmsse": level_score,
            "node_count": group_count,
            "weight_sum": float(node_revenue.sum()),
        }

    return {
        "metric": "M5 WRMSSE",
        "wrmsse": sum(value["weighted_rmsse"] for value in level_results.values())
        / len(M5_HIERARCHY_LEVELS),
        "hierarchy_level_count": len(M5_HIERARCHY_LEVELS),
        "bottom_series_count": len(ids),
        "training_days": training_days,
        "horizon_days": horizon,
        "weight_definition": "28-day pre-origin unit sales multiplied by sell_price, normalized across bottom series",
        "levels": level_results,
        "scope_warning": (
            "This is the full-M5 metric only when inputs contain every M5 bottom-level series; "
            "a filtered subset is not an official M5 WRMSSE score."
        ),
    }


def _group_indices(
    ids: Sequence[str],
    metadata: Mapping[str, Sequence[str]],
    fields: Sequence[str],
) -> tuple[np.ndarray, int]:
    group_keys = [tuple(metadata[field][index] for field in fields) for index in range(len(ids))]
    if not fields:
        group_keys = [("Total",)] * len(ids)
    key_to_index: dict[tuple[str, ...], int] = {}
    indices = np.empty(len(ids), dtype=np.int64)
    for row_number, key in enumerate(group_keys):
        if key not in key_to_index:
            key_to_index[key] = len(key_to_index)
        indices[row_number] = key_to_index[key]
    return indices, len(key_to_index)


def _m5_scales(history: np.ndarray, revenue_weights: np.ndarray) -> np.ndarray:
    scales = np.zeros(history.shape[0], dtype=np.float64)
    for row_number, values in enumerate(history):
        nonzero_positions = np.flatnonzero(values)
        if len(nonzero_positions) < 2:
            if revenue_weights[row_number] > 0:
                raise ValueError("a positive-weight M5 node has no non-zero training scale")
            continue
        active_values = values[nonzero_positions[0]:]
        scale = float(np.mean(np.square(np.diff(active_values))))
        if scale <= 0 or not math.isfinite(scale):
            if revenue_weights[row_number] > 0:
                raise ValueError("a positive-weight M5 node has a zero or invalid RMSSE scale")
            continue
        scales[row_number] = scale
    if (scales[revenue_weights > 0] <= 0).any():
        raise ValueError("all positive-weight hierarchy nodes need a non-zero RMSSE scale")
    scales[revenue_weights == 0] = 1.0
    return scales
