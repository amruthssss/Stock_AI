"""Optional adapters for zero-shot forecast models with native quantiles.

Model libraries are imported only by the explicit loaders so that the core
StockWise environment remains usable without downloading large ML packages.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.forecasting.baselines import build_evaluation_windows, calculate_metrics
from backend.forecasting.interval_metrics import calculate_interval_metrics


QUANTILE_LEVELS = (0.1, 0.5, 0.9)
TIMESFM_QUANTILE_COLUMNS = (1, 5, 9)  # output is mean, q10, q20, ..., q90


class FoundationModelUnavailableError(RuntimeError):
    """Raised when an optional pretrained-model package is not installed."""


def load_chronos2(*, model_id: str = "amazon/chronos-2", device: str = "cpu") -> Any:
    """Load Amazon Chronos-2 on the requested device (downloads weights if absent)."""
    try:
        from chronos import Chronos2Pipeline
    except ImportError as error:
        raise FoundationModelUnavailableError(
            "Chronos-2 is optional. Install requirements-foundation-models.txt first."
        ) from error
    return Chronos2Pipeline.from_pretrained(model_id, device_map=device)


def load_timesfm25(
    *,
    model_id: str = "google/timesfm-2.5-200m-pytorch",
    max_context: int = 1024,
    horizon: int = 28,
    batch_size: int = 8,
) -> Any:
    """Load and compile TimesFM 2.5 with positive, ordered native quantiles."""
    try:
        import timesfm
    except ImportError as error:
        raise FoundationModelUnavailableError(
            "TimesFM 2.5 is optional. Install requirements-foundation-models.txt first."
        ) from error

    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(model_id)
    model.compile(
        timesfm.ForecastConfig(
            max_context=max_context,
            max_horizon=horizon,
            normalize_inputs=True,
            per_core_batch_size=batch_size,
            use_continuous_quantile_head=True,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
        )
    )
    return model


def forecast_chronos2(
    pipeline: Any,
    histories: Mapping[str, Sequence[float]],
    *,
    horizon: int,
    batch_size: int = 64,
) -> dict[str, dict[str, list[float]]]:
    """Return q10/median/q90 forecasts for each series using Chronos-2."""
    _validate_request(histories, horizon)
    ordered_ids = sorted(histories)
    contexts = [[float(value) for value in histories[key]] for key in ordered_ids]
    quantile_batches, _means = pipeline.predict_quantiles(
        inputs=contexts,
        prediction_length=horizon,
        quantile_levels=list(QUANTILE_LEVELS),
        batch_size=batch_size,
    )
    if len(quantile_batches) != len(ordered_ids):
        raise RuntimeError("Chronos-2 returned a different number of series than requested")

    results: dict[str, dict[str, list[float]]] = {}
    for series_id, raw_quantiles in zip(ordered_ids, quantile_batches, strict=True):
        values = _as_python_list(raw_quantiles)
        if len(values) == 1 and isinstance(values[0], list):
            values = values[0]
        if len(values) != horizon or any(len(row) != len(QUANTILE_LEVELS) for row in values):
            raise RuntimeError("Chronos-2 returned an unexpected quantile forecast shape")
        results[series_id] = _validated_quantile_rows(values, series_id)
    return results


def forecast_timesfm25(
    model: Any,
    histories: Mapping[str, Sequence[float]],
    *,
    horizon: int,
) -> dict[str, dict[str, list[float]]]:
    """Return q10/median/q90 forecasts for each series using TimesFM 2.5."""
    _validate_request(histories, horizon)
    import numpy as np

    ordered_ids = sorted(histories)
    contexts = [np.asarray(histories[key], dtype=np.float32) for key in ordered_ids]
    _point, all_quantiles = model.forecast(horizon=horizon, inputs=contexts)
    quantile_array = _as_numpy(all_quantiles)
    if quantile_array.ndim != 3 or quantile_array.shape[:2] != (len(ordered_ids), horizon):
        raise RuntimeError("TimesFM 2.5 returned an unexpected quantile forecast shape")
    if quantile_array.shape[2] <= TIMESFM_QUANTILE_COLUMNS[-1]:
        raise RuntimeError("TimesFM 2.5 output does not include q10, median, and q90")

    results: dict[str, dict[str, list[float]]] = {}
    for row_number, series_id in enumerate(ordered_ids):
        rows = [
            [float(quantile_array[row_number, day, column]) for column in TIMESFM_QUANTILE_COLUMNS]
            for day in range(horizon)
        ]
        results[series_id] = _validated_quantile_rows(rows, series_id)
    return results


def _validate_request(histories: Mapping[str, Sequence[float]], horizon: int) -> None:
    if not histories:
        raise ValueError("at least one time series is required")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    expected_length = len(next(iter(histories.values())))
    if expected_length < 1:
        raise ValueError("history must contain at least one observation")
    for series_id, values in histories.items():
        if len(values) != expected_length:
            raise ValueError(f"series {series_id} has a different history length")
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
            raise ValueError(f"series {series_id} has invalid demand history")


def _as_python_list(values: Any) -> list[Any]:
    """Convert nested NumPy/PyTorch output to Python lists without importing either."""
    if hasattr(values, "detach"):
        values = values.detach().cpu().numpy()
    if hasattr(values, "tolist"):
        return values.tolist()
    return list(values)


def _as_numpy(values: Any) -> Any:
    """Convert a TimesFM output tensor to NumPy through its public array interface."""
    if hasattr(values, "detach"):
        values = values.detach().cpu().numpy()
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - Polars already requires NumPy
        raise FoundationModelUnavailableError("NumPy is required by the installed TimesFM model") from error
    return np.asarray(values)


def _validated_quantile_rows(
    rows: Sequence[Sequence[float]], series_id: str
) -> dict[str, list[float]]:
    lower: list[float] = []
    median: list[float] = []
    upper: list[float] = []
    for row in rows:
        if len(row) != 3:
            raise RuntimeError("A native quantile forecast must contain q10, q50, and q90")
        values = [float(value) for value in row]
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError(f"Model returned a non-finite quantile for {series_id}")
        ordered = sorted(max(value, 0.0) for value in values)
        lower.append(ordered[0])
        median.append(ordered[1])
        upper.append(ordered[2])
    return {"q10": lower, "q50": median, "q90": upper}


def evaluate_native_quantiles(
    model: Any,
    series_by_id: Mapping[str, Sequence[float]],
    *,
    model_name: str,
    forecast_function: Callable[..., dict[str, dict[str, list[float]]]],
    initial_window: int = 365,
    horizon: int = 28,
    folds: int = 3,
) -> dict[str, Any]:
    """Evaluate one native-quantile model on the shared chronological windows."""
    if not series_by_id:
        raise ValueError("at least one time series is required")
    series_length = len(next(iter(series_by_id.values())))
    for series_id, values in series_by_id.items():
        if len(values) != series_length:
            raise ValueError(f"series {series_id} has a different history length")
    origins, heldout_start = build_evaluation_windows(
        series_length,
        initial_window=initial_window,
        horizon=horizon,
        folds=folds,
    )

    fold_results: list[dict[str, Any]] = []
    rolling_actual: list[float] = []
    rolling_point: list[float] = []
    rolling_q10: list[float] = []
    rolling_q90: list[float] = []
    diagnostics: list[dict[str, Any]] = []
    ordered_ids = sorted(series_by_id)

    for fold_number, origin in enumerate(origins, start=1):
        actual, quantiles, seconds = _forecast_window(
            model, series_by_id, ordered_ids, origin, horizon, forecast_function
        )
        point = quantiles["q50"]
        rolling_actual.extend(actual)
        rolling_point.extend(point)
        rolling_q10.extend(quantiles["q10"])
        rolling_q90.extend(quantiles["q90"])
        fold_results.append({
            "fold": fold_number,
            "train_end_index_exclusive": origin,
            "test_start_index": origin,
            "test_end_index_exclusive": origin + horizon,
            "metrics": calculate_metrics(actual, point),
            "direct_quantile_metrics": calculate_interval_metrics(
                actual, quantiles["q10"], quantiles["q90"], alpha=0.2
            ),
        })
        diagnostics.append({"fold": fold_number, "prediction_seconds": seconds})

    heldout_actual, heldout_quantiles, heldout_seconds = _forecast_window(
        model, series_by_id, ordered_ids, heldout_start, horizon, forecast_function
    )
    diagnostics.append({"period": "held_out", "prediction_seconds": heldout_seconds})
    rolling_metrics = calculate_metrics(rolling_actual, rolling_point)
    heldout_metrics = calculate_metrics(heldout_actual, heldout_quantiles["q50"])
    return {
        "model": model_name,
        "forecast_type": "native q10/q50/q90; point metrics use q50",
        "series_count": len(series_by_id),
        "observations_per_series": series_length,
        "rolling_origin": {
            "folds": fold_results,
            "metrics": rolling_metrics,
            "direct_quantile_metrics": calculate_interval_metrics(
                rolling_actual, rolling_q10, rolling_q90, alpha=0.2
            ),
        },
        "held_out": {
            "train_end_index_exclusive": heldout_start,
            "test_start_index": heldout_start,
            "test_end_index_exclusive": series_length,
            "metrics": heldout_metrics,
            "direct_quantile_metrics": calculate_interval_metrics(
                heldout_actual,
                heldout_quantiles["q10"],
                heldout_quantiles["q90"],
                alpha=0.2,
            ),
        },
        "diagnostics": diagnostics,
    }


def _forecast_window(
    model: Any,
    series_by_id: Mapping[str, Sequence[float]],
    ordered_ids: Sequence[str],
    origin: int,
    horizon: int,
    forecast_function: Callable[..., dict[str, dict[str, list[float]]]],
) -> tuple[list[float], dict[str, list[float]], float]:
    import time

    histories = {series_id: series_by_id[series_id][:origin] for series_id in ordered_ids}
    started = time.perf_counter()
    forecasts = forecast_function(model, histories, horizon=horizon)
    elapsed = time.perf_counter() - started
    if set(forecasts) != set(ordered_ids):
        raise RuntimeError("Model returned missing or unexpected series IDs")

    flattened = {name: [] for name in ("q10", "q50", "q90")}
    actual: list[float] = []
    for series_id in ordered_ids:
        values = forecasts[series_id]
        for name in flattened:
            prediction = values.get(name)
            if prediction is None or len(prediction) != horizon:
                raise RuntimeError(f"Model returned an invalid {name} horizon for {series_id}")
            flattened[name].extend(float(value) for value in prediction)
        actual.extend(float(value) for value in series_by_id[series_id][origin:origin + horizon])
    return actual, flattened, elapsed

