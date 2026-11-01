"""Empirically calibrated prediction bands and quantile metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


def empirical_quantile(values: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated empirical quantile for finite values."""
    if not values:
        raise ValueError("at least one value is required to calculate a quantile")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("quantile values must be finite")
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


def _pinball_loss(actual: float, forecast_quantile: float, quantile: float) -> float:
    error = actual - forecast_quantile
    return quantile * error if error >= 0 else (quantile - 1) * error


def calculate_interval_metrics(
    actual: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
    *,
    alpha: float,
) -> dict[str, Any]:
    """Calculate coverage, width, pinball losses, and weighted quantile loss."""
    if len(actual) == 0 or len(actual) != len(lower) or len(actual) != len(upper):
        raise ValueError("actual, lower, and upper must have the same non-zero length")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be greater than 0 and less than 1")
    actual_values = [float(value) for value in actual]
    lower_values = [float(value) for value in lower]
    upper_values = [float(value) for value in upper]
    if any(not math.isfinite(value) or value < 0 for value in actual_values):
        raise ValueError("actual values must be finite and non-negative")
    if any(not math.isfinite(value) or value < 0 for value in lower_values + upper_values):
        raise ValueError("interval bounds must be finite and non-negative")
    if any(low > high for low, high in zip(lower_values, upper_values, strict=True)):
        raise ValueError("lower interval bounds cannot exceed upper bounds")

    lower_quantile = alpha / 2
    upper_quantile = 1 - alpha / 2
    lower_pinball_sum = sum(
        _pinball_loss(observed, prediction, lower_quantile)
        for observed, prediction in zip(actual_values, lower_values, strict=True)
    )
    upper_pinball_sum = sum(
        _pinball_loss(observed, prediction, upper_quantile)
        for observed, prediction in zip(actual_values, upper_values, strict=True)
    )
    actual_total = sum(abs(value) for value in actual_values)
    lower_pinball = lower_pinball_sum / len(actual_values)
    upper_pinball = upper_pinball_sum / len(actual_values)
    weighted_quantile_loss = (
        (2 * lower_pinball_sum / actual_total + 2 * upper_pinball_sum / actual_total) / 2
        if actual_total else None
    )
    return {
        "nominal_coverage": 1 - alpha,
        "empirical_coverage": sum(
            low <= observed <= high
            for observed, low, high in zip(actual_values, lower_values, upper_values, strict=True)
        ) / len(actual_values),
        "mean_interval_width": sum(
            high - low for low, high in zip(lower_values, upper_values, strict=True)
        ) / len(actual_values),
        "lower_quantile": lower_quantile,
        "lower_pinball_loss": lower_pinball,
        "upper_quantile": upper_quantile,
        "upper_pinball_loss": upper_pinball,
        "weighted_quantile_loss": weighted_quantile_loss,
        "observations": len(actual_values),
    }


def evaluate_prequential_intervals(
    fold_predictions: Sequence[tuple[Sequence[float], Sequence[float]]],
    heldout_actual: Sequence[float],
    heldout_point_forecast: Sequence[float],
    *,
    alpha: float = 0.2,
) -> dict[str, Any]:
    """Calibrate each fold from earlier fold errors and holdout from all folds.

    Each pair in fold_predictions is `(actual, point_forecast)` for one
    chronological fold. A fold never contributes its own errors to its band.
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha must be greater than 0 and less than 1")
    if not fold_predictions:
        raise ValueError("at least one rolling-origin fold is required for calibration")

    calibration_errors: list[float] = []
    rolling_actual: list[float] = []
    rolling_lower: list[float] = []
    rolling_upper: list[float] = []
    fold_results: list[dict[str, Any]] = []

    for fold_number, (fold_actual, fold_point) in enumerate(fold_predictions, start=1):
        if len(fold_actual) == 0 or len(fold_actual) != len(fold_point):
            raise ValueError("each fold's actual and point forecast must have equal non-zero length")
        fold_actual_values = [float(value) for value in fold_actual]
        fold_point_values = [float(value) for value in fold_point]
        if any(not math.isfinite(value) or value < 0 for value in fold_actual_values):
            raise ValueError("fold actuals must be finite and non-negative")
        if any(not math.isfinite(value) or value < 0 for value in fold_point_values):
            raise ValueError("fold forecasts must be finite and non-negative")
        if not calibration_errors:
            fold_results.append({
                "fold": fold_number,
                "status": "not_calibrated",
                "calibration_residual_count": 0,
                "reason": "no earlier out-of-fold residuals are available",
            })
        else:
            lower_error = empirical_quantile(calibration_errors, alpha / 2)
            upper_error = empirical_quantile(calibration_errors, 1 - alpha / 2)
            lower = [max(0.0, float(point) + lower_error) for point in fold_point]
            upper = [max(0.0, float(point) + upper_error) for point in fold_point]
            fold_metrics = calculate_interval_metrics(fold_actual, lower, upper, alpha=alpha)
            fold_results.append({
                "fold": fold_number,
                "status": "completed",
                "calibration_residual_count": len(calibration_errors),
                "calibration_residual_quantiles": {
                    "lower": lower_error,
                    "upper": upper_error,
                },
                "metrics": fold_metrics,
            })
            rolling_actual.extend(float(value) for value in fold_actual)
            rolling_lower.extend(lower)
            rolling_upper.extend(upper)

        calibration_errors.extend(
            actual_value - point_value
            for actual_value, point_value in zip(fold_actual_values, fold_point_values, strict=True)
        )

    if not heldout_actual or len(heldout_actual) != len(heldout_point_forecast):
        raise ValueError("held-out actuals and point forecast must have equal non-zero length")
    heldout_actual_values = [float(value) for value in heldout_actual]
    heldout_point_values = [float(value) for value in heldout_point_forecast]
    if any(not math.isfinite(value) or value < 0 for value in heldout_actual_values):
        raise ValueError("held-out actuals must be finite and non-negative")
    if any(not math.isfinite(value) or value < 0 for value in heldout_point_values):
        raise ValueError("held-out forecasts must be finite and non-negative")

    lower_error = empirical_quantile(calibration_errors, alpha / 2)
    upper_error = empirical_quantile(calibration_errors, 1 - alpha / 2)
    heldout_lower = [max(0.0, point + lower_error) for point in heldout_point_values]
    heldout_upper = [max(0.0, point + upper_error) for point in heldout_point_values]
    return {
        "method": "empirical quantiles of earlier out-of-fold residuals; lower bounds clipped at zero",
        "nominal_coverage": 1 - alpha,
        "alpha": alpha,
        "rolling_origin": {
            "folds": fold_results,
            "calibrated_fold_count": sum(row["status"] == "completed" for row in fold_results),
            "metrics": calculate_interval_metrics(rolling_actual, rolling_lower, rolling_upper, alpha=alpha)
            if rolling_actual else None,
        },
        "held_out": {
            "calibration_residual_count": len(calibration_errors),
            "calibration_residual_quantiles": {
                "lower": lower_error,
                "upper": upper_error,
            },
            "metrics": calculate_interval_metrics(
                heldout_actual_values, heldout_lower, heldout_upper, alpha=alpha
            ),
        },
        "limitation": (
            "Empirical historical-residual calibration is descriptive and does not guarantee "
            "nominal coverage for dependent or changing demand series."
        ),
    }
