"""Evaluate the four simple baselines using the complete M5 hierarchy WRMSSE."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from stockwise.forecasting.wrmsse import calculate_m5_wrmsse_arrays


METADATA_COLUMNS = ("id", "item_id", "dept_id", "cat_id", "store_id", "state_id")
EXPECTED_M5_BOTTOM_SERIES = 30_490


def _baseline_forecast_matrices(history: np.ndarray, horizon: int) -> dict[str, np.ndarray]:
    """Vectorized held-out forecasts for the four established baseline models."""
    if history.ndim != 2 or history.shape[0] == 0 or history.shape[1] < 28:
        raise ValueError("history must be a non-empty matrix with at least 28 days")
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if not np.isfinite(history).all() or (history < 0).any():
        raise ValueError("M5 history must be finite and non-negative")

    row_count = history.shape[0]
    seasonal = np.tile(history[:, -7:], (1, (horizon + 6) // 7))[:, :horizon]
    recent_level = history[:, -28:].mean(axis=1)
    recent = np.repeat(recent_level[:, None], horizon, axis=1)

    ets_level = history[:, 0].astype(np.float64, copy=True)
    for day_index in range(1, history.shape[1]):
        ets_level = 0.2 * history[:, day_index] + 0.8 * ets_level
    ets = np.repeat(ets_level[:, None], horizon, axis=1)

    nonzero = history > 0
    has_demand = nonzero.any(axis=1)
    first_demand = np.argmax(nonzero, axis=1)
    demand_size = history[np.arange(row_count), first_demand].astype(np.float64, copy=True)
    demand_interval = (first_demand + 1).astype(np.float64)
    last_demand = first_demand.copy()
    for day_index in range(1, history.shape[1]):
        observed = history[:, day_index]
        active = (day_index > first_demand) & (observed > 0)
        interval = day_index - last_demand
        demand_size[active] = 0.1 * observed[active] + 0.9 * demand_size[active]
        demand_interval[active] = 0.1 * interval[active] + 0.9 * demand_interval[active]
        last_demand[active] = day_index
    croston_rate = (1 - 0.1 / 2) * demand_size / demand_interval
    croston_rate[~has_demand] = 0.0
    croston = np.repeat(croston_rate[:, None], horizon, axis=1)

    return {
        "seasonal_naive": seasonal.astype(np.float64),
        "recent_average": recent,
        "ets_simple": ets,
        "croston_sba": croston,
    }


def _build_revenue_weights(
    sales_matrix: np.ndarray,
    day_columns: list[str],
    metadata_rows: list[dict[str, str]],
    calendar_week_by_day: dict[str, int],
    price_by_key: dict[tuple[str, str, int], float],
    *,
    train_end_index_exclusive: int,
    window_days: int = 28,
) -> np.ndarray:
    """Build M5 dollar-sales weights from the 28 training days before origin."""
    if train_end_index_exclusive < window_days or train_end_index_exclusive > len(day_columns):
        raise ValueError("training origin does not include the requested revenue-weight window")
    if sales_matrix.shape[0] != len(metadata_rows):
        raise ValueError("sales rows and M5 metadata rows must match")

    row_count = sales_matrix.shape[0]
    revenue = np.zeros(row_count, dtype=np.float64)
    item_store = [
        (str(row["item_id"]), str(row["store_id"])) for row in metadata_rows
    ]
    for day_index in range(train_end_index_exclusive - window_days, train_end_index_exclusive):
        day = day_columns[day_index]
        if day not in calendar_week_by_day:
            raise ValueError(f"M5 calendar has no week mapping for {day}")
        week = calendar_week_by_day[day]
        prices = np.empty(row_count, dtype=np.float64)
        for row_number, (item_id, store_id) in enumerate(item_store):
            price = price_by_key.get((item_id, store_id, week))
            sold_units = float(sales_matrix[row_number, day_index])
            if price is None and sold_units > 0:
                raise ValueError(f"M5 sell price is missing for sold series {item_id}/{store_id}/{week}")
            prices[row_number] = 0.0 if price is None else price
        revenue += sales_matrix[:, day_index] * prices

    if float(revenue.sum()) <= 0:
        raise ValueError("M5 revenue weights are all zero for the selected training window")
    return revenue


def run_full_m5_wrmsse(project_root: Path, *, horizon: int = 28) -> Path:
    """Run held-out WRMSSE for all M5 bottom series and four simple baselines."""
    if horizon != 28:
        raise ValueError("the M5 accuracy evaluation horizon is 28 days")
    project_root = Path(project_root)
    m5_directory = project_root / "data" / "processed" / "m5"
    try:
        latest = json.loads((m5_directory / "latest.json").read_text(encoding="utf-8"))
        report_path = (m5_directory / latest["quality_report"]).resolve()
        report_path.relative_to(m5_directory.resolve())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        run_directory = (m5_directory / latest["run_directory"]).resolve()
        run_directory.relative_to(m5_directory.resolve())
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not resolve a safe, valid M5 ingestion run: {error}") from error
    if report.get("status") != "passed":
        raise ValueError("Latest M5 ingestion report is not marked passed")

    sales_path = run_directory / "sales_train_validation.parquet"
    calendar_path = run_directory / "calendar.parquet"
    prices_path = run_directory / "sell_prices.parquet"
    try:
        sales_frame = pl.read_parquet(sales_path).sort("id")
        calendar = pl.read_parquet(calendar_path).select("d", "wm_yr_wk")
    except Exception as error:
        raise ValueError(f"Could not read full M5 Parquet inputs: {error}") from error
    missing_columns = set(METADATA_COLUMNS).difference(sales_frame.columns)
    if missing_columns:
        raise ValueError(f"M5 sales Parquet is missing metadata columns: {sorted(missing_columns)}")
    day_columns = sorted(
        (name for name in sales_frame.columns if name.startswith("d_")),
        key=lambda name: int(name[2:]),
    )
    if len(day_columns) <= horizon:
        raise ValueError("M5 sales history is too short for a held-out evaluation")
    if sales_frame.height != EXPECTED_M5_BOTTOM_SERIES:
        raise ValueError(
            f"Full-M5 WRMSSE requires {EXPECTED_M5_BOTTOM_SERIES} bottom series; "
            f"found {sales_frame.height}"
        )
    metadata_rows = sales_frame.select(METADATA_COLUMNS).to_dicts()
    series_ids = [str(row["id"]) for row in metadata_rows]
    sales_matrix = sales_frame.select(day_columns).to_numpy()
    heldout_start = len(day_columns) - horizon
    history = sales_matrix[:, :heldout_start]
    actuals = sales_matrix[:, heldout_start:]

    try:
        calendar_week_by_day = {
            str(day): int(week)
            for day, week in calendar.iter_rows()
        }
        weight_weeks = sorted({
            calendar_week_by_day[day]
            for day in day_columns[heldout_start - 28:heldout_start]
        })
        price_rows = (
            pl.scan_parquet(prices_path)
            .filter(pl.col("wm_yr_wk").is_in(weight_weeks))
            .select("item_id", "store_id", "wm_yr_wk", "sell_price")
            .collect(engine="streaming")
            .iter_rows()
        )
        price_by_key = {
            (str(item), str(store), int(week)): float(price)
            for item, store, week, price in price_rows
        }
    except Exception as error:
        raise ValueError(f"Could not build the M5 28-day revenue weights: {error}") from error
    revenue_weights = _build_revenue_weights(
        sales_matrix,
        day_columns,
        metadata_rows,
        calendar_week_by_day,
        price_by_key,
        train_end_index_exclusive=heldout_start,
    )

    metadata_by_id = {str(row["id"]): row for row in metadata_rows}
    model_results: dict[str, Any] = {}
    for model_name, forecasts in _baseline_forecast_matrices(history, horizon).items():
        model_results[model_name] = calculate_m5_wrmsse_arrays(
            series_ids,
            history,
            actuals,
            forecasts,
            metadata_by_id,
            revenue_weights,
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    output = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "data_provenance": {
            "source": "observed public M5 sales history and M5 sell prices",
            "synthetic_data_included": False,
            "m5_ingestion_run_id": latest["run_id"],
        },
        "evaluation": {
            "scope": "all 30,490 M5 bottom-level series; full 12-level hierarchy",
            "training_days": heldout_start,
            "held_out_days": horizon,
            "weight_window_days": 28,
            "held_out_start_day": day_columns[heldout_start],
            "held_out_end_day": day_columns[-1],
            "models": model_results,
        },
        "limitations": [
            "This is a chronological internal holdout using M5's WRMSSE hierarchy and revenue-weight definition; it is not the competition leaderboard score.",
            "M5 records observed sales, which can be censored by stockouts.",
            "The result covers the four simple baselines only; tree and foundation model WRMSSE runs are not included.",
        ],
    }
    output_directory = project_root / "data" / "processed" / "forecasts" / "wrmsse"
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"wrmsse_{run_id}.json"
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            json.dump(output, output_file, indent=2, ensure_ascii=False, allow_nan=False)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Score four baselines using full-M5 WRMSSE.")
    parser.parse_args()
    try:
        result_path = run_full_m5_wrmsse(project_root)
    except (OSError, ValueError) as error:
        print(f"Full-M5 WRMSSE evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"Full-M5 WRMSSE evaluation completed. JSON run record: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

