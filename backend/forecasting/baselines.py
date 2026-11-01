"""Simple demand forecasting baselines and chronological evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from backend.forecasting.interval_metrics import evaluate_prequential_intervals


def _validate_forecast_request(history: Sequence[float], horizon: int) -> None:
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if len(history) == 0:
        raise ValueError("history must contain at least one observation")
    for value in history:
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError("history values must be finite and non-negative")


def seasonal_naive(
    history: Sequence[float],
    horizon: int,
    season_length: int = 7,
) -> list[float]:
    """Repeat the latest observed season into the future."""
    _validate_forecast_request(history, horizon)
    if season_length < 1:
        raise ValueError("season_length must be at least 1")
    if len(history) < season_length:
        raise ValueError("history must be at least one full season long")

    last_season = [float(value) for value in history[-season_length:]]
    repeats = (horizon + season_length - 1) // season_length
    return (last_season * repeats)[:horizon]


def recent_average(
    history: Sequence[float],
    horizon: int,
    window: int = 28,
) -> list[float]:
    """Repeat the mean demand from the most recent window."""
    _validate_forecast_request(history, horizon)
    if window < 1:
        raise ValueError("window must be at least 1")
    if len(history) < window:
        raise ValueError("history must be at least as long as window")

    recent_values = history[-window:]
    average = sum(float(value) for value in recent_values) / window
    return [average] * horizon


def ets_simple(
    history: Sequence[float],
    horizon: int,
    alpha: float = 0.2,
) -> list[float]:
    """Simple exponential smoothing (ETS(A,N,N)) with a fixed alpha."""
    _validate_forecast_request(history, horizon)
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be greater than 0 and at most 1")

    level = float(history[0])
    for observed in history[1:]:
        level = alpha * float(observed) + (1 - alpha) * level
    return [level] * horizon


def croston_sba(
    history: Sequence[float],
    horizon: int,
    alpha: float = 0.1,
) -> list[float]:
    """Croston's intermittent-demand rate with the Syntetos-Boylan correction."""
    _validate_forecast_request(history, horizon)
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be greater than 0 and at most 1")

    first_demand_index = next(
        (index for index, value in enumerate(history) if value > 0),
        None,
    )
    if first_demand_index is None:
        return [0.0] * horizon

    demand_size = float(history[first_demand_index])
    demand_interval = float(first_demand_index + 1)
    last_demand_index = first_demand_index
    for index in range(first_demand_index + 1, len(history)):
        observed = float(history[index])
        if observed > 0:
            interval = float(index - last_demand_index)
            demand_size = alpha * observed + (1 - alpha) * demand_size
            demand_interval = alpha * interval + (1 - alpha) * demand_interval
            last_demand_index = index

    corrected_rate = (1 - alpha / 2) * demand_size / demand_interval
    return [corrected_rate] * horizon


def calculate_metrics(actual: Sequence[float], forecast: Sequence[float]) -> dict[str, Any]:
    """Return MAE, WAPE, and relative bias with explicit zero-demand handling."""
    if len(actual) == 0 or len(actual) != len(forecast):
        raise ValueError("actual and forecast must have the same non-zero length")

    absolute_error = sum(abs(float(predicted) - float(observed))
                         for observed, predicted in zip(actual, forecast))
    actual_total = sum(abs(float(value)) for value in actual)
    forecast_delta = sum(float(predicted) - float(observed)
                         for observed, predicted in zip(actual, forecast))
    return {
        "mae": absolute_error / len(actual),
        "wape": absolute_error / actual_total if actual_total else None,
        "bias": forecast_delta / actual_total if actual_total else None,
        "observations": len(actual),
    }


def build_evaluation_windows(
    series_length: int,
    *,
    initial_window: int,
    horizon: int,
    folds: int,
) -> tuple[list[int], int]:
    """Return rolling-origin cutoffs and the final held-out start index."""
    if initial_window < 1 or horizon < 1 or folds < 1:
        raise ValueError("initial_window, horizon, and folds must be at least 1")
    heldout_start = series_length - horizon
    first_origin = heldout_start - folds * horizon
    if first_origin < initial_window:
        needed_length = initial_window + ((folds + 1) * horizon)
        raise ValueError(
            f"series length {series_length} is too short; need at least {needed_length} "
            "observations for the requested rolling folds and held-out window"
        )
    origins = [first_origin + index * horizon for index in range(folds)]
    return origins, heldout_start


def _forecast_models(
    history: Sequence[float],
    horizon: int,
    recent_window: int,
    season_length: int,
    ets_alpha: float,
    croston_alpha: float,
) -> dict[str, list[float]]:
    return {
        "seasonal_naive": seasonal_naive(history, horizon, season_length),
        "recent_average": recent_average(history, horizon, recent_window),
        "ets_simple": ets_simple(history, horizon, ets_alpha),
        "croston_sba": croston_sba(history, horizon, croston_alpha),
    }


def evaluate_baselines(
    series_by_id: Mapping[str, Sequence[float]],
    *,
    initial_window: int = 365,
    horizon: int = 28,
    folds: int = 3,
    recent_window: int = 28,
    season_length: int = 7,
    ets_alpha: float = 0.2,
    croston_alpha: float = 0.1,
) -> dict[str, Any]:
    """Compare both baselines on identical rolling folds and a final holdout.

    All indices are zero-based and the end indices are exclusive. The final
    `horizon` observations are reserved for the held-out evaluation; the
    rolling-origin folds all occur before that holdout.
    """
    if not series_by_id:
        raise ValueError("at least one series is required")
    if recent_window < 1 or season_length < 1:
        raise ValueError("recent_window and season_length must be at least 1")
    if not 0 < ets_alpha <= 1 or not 0 < croston_alpha <= 1:
        raise ValueError("ETS and Croston alpha values must be greater than 0 and at most 1")
    if recent_window > initial_window or season_length > initial_window:
        raise ValueError("baseline windows must not exceed initial_window")

    first_series = next(iter(series_by_id.values()))
    series_length = len(first_series)
    needed_length = initial_window + ((folds + 1) * horizon)
    if series_length < needed_length:
        raise ValueError(
            f"series length {series_length} is too short; need at least {needed_length} "
            "observations for the requested rolling folds and held-out window"
        )

    for series_id, values in series_by_id.items():
        if len(values) != series_length:
            raise ValueError(f"series {series_id} has {len(values)} observations; expected {series_length}")
        _validate_forecast_request(values, series_length)

    rolling_origins, heldout_start = build_evaluation_windows(
        series_length,
        initial_window=initial_window,
        horizon=horizon,
        folds=folds,
    )
    model_names = ["seasonal_naive", "recent_average", "ets_simple", "croston_sba"]
    rolling_actual: dict[str, list[float]] = {name: [] for name in model_names}
    rolling_predictions: dict[str, list[float]] = {name: [] for name in model_names}
    fold_actual_forecasts: dict[str, list[tuple[list[float], list[float]]]] = {
        name: [] for name in model_names
    }
    fold_results: list[dict[str, Any]] = []

    for fold_number, origin in enumerate(rolling_origins, start=1):
        fold_actuals: list[float] = []
        fold_predictions: dict[str, list[float]] = {name: [] for name in model_names}
        for values in series_by_id.values():
            actual = [float(value) for value in values[origin:origin + horizon]]
            forecasts = _forecast_models(
                values[:origin], horizon, recent_window, season_length,
                ets_alpha, croston_alpha,
            )
            fold_actuals.extend(actual)
            for name in model_names:
                fold_predictions[name].extend(forecasts[name])
                rolling_actual[name].extend(actual)
                rolling_predictions[name].extend(forecasts[name])

        fold_results.append({
            "fold": fold_number,
            "train_end_index_exclusive": origin,
            "test_start_index": origin,
            "test_end_index_exclusive": origin + horizon,
            "models": {
                name: calculate_metrics(fold_actuals, fold_predictions[name])
                for name in model_names
            },
        })
        for name in model_names:
            fold_actual_forecasts[name].append(
                (fold_actuals.copy(), fold_predictions[name].copy())
            )

    heldout_actual: list[float] = []
    heldout_predictions: dict[str, list[float]] = {name: [] for name in model_names}
    for values in series_by_id.values():
        actual = [float(value) for value in values[heldout_start:]]
        forecasts = _forecast_models(
            values[:heldout_start], horizon, recent_window, season_length,
            ets_alpha, croston_alpha,
        )
        heldout_actual.extend(actual)
        for name in model_names:
            heldout_predictions[name].extend(forecasts[name])

    interval_results = {
        name: evaluate_prequential_intervals(
            fold_actual_forecasts[name], heldout_actual, heldout_predictions[name], alpha=0.2
        )
        for name in model_names
    }

    return {
        "series_count": len(series_by_id),
        "observations_per_series": series_length,
        "parameters": {
            "initial_window": initial_window,
            "horizon": horizon,
            "folds": folds,
            "recent_average_window": recent_window,
            "seasonal_period": season_length,
            "ets_alpha": ets_alpha,
            "croston_sba_alpha": croston_alpha,
        },
        "model_definitions": {
            "seasonal_naive": "repeat the last seven observed days",
            "recent_average": "repeat the mean of the latest configured window",
            "ets_simple": "ETS(A,N,N), simple exponential smoothing with fixed alpha",
            "croston_sba": "smoothed non-zero demand size / interval with Syntetos-Boylan correction",
        },
        "rolling_origin": {
            "folds": fold_results,
            "models": {
                name: calculate_metrics(rolling_actual[name], rolling_predictions[name])
                for name in model_names
            },
        },
        "held_out": {
            "train_end_index_exclusive": heldout_start,
            "test_start_index": heldout_start,
            "test_end_index_exclusive": series_length,
            "models": {
                name: calculate_metrics(heldout_actual, heldout_predictions[name])
                for name in model_names
            },
        },
        "uncertainty_intervals": interval_results,
        "metric_definitions": {
            "mae": "mean absolute error in units per series-day",
            "wape": "sum absolute error / sum absolute actual; null when actual denominator is zero",
            "bias": "sum(forecast - actual) / sum absolute actual; positive means over-forecast; null when denominator is zero",
            "aggregation": "micro-averaged across all selected series and evaluation days",
            "intervals": (
                "nominal 80% bands from 0.1 and 0.9 empirical quantiles of earlier rolling-origin "
                "residuals; fold 1 is not scored because no prior calibration residuals exist"
            ),
            "pinball_loss": "quantile loss rho_q(y - forecast_q), reported for q=0.1 and q=0.9 endpoints",
            "weighted_quantile_loss": (
                "mean over q=0.1 and q=0.9 of 2 * summed pinball loss / summed absolute actuals; "
                "null when the actual denominator is zero"
            ),
        },
    }
