"""Run the deterministic baseline comparison on an M5 Parquet subset."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from stockwise.forecasting.baselines import evaluate_baselines
from stockwise.forecasting.foundation_models import (
    evaluate_native_quantiles,
    forecast_chronos2,
    forecast_timesfm25,
    load_chronos2,
    load_timesfm25,
)
from stockwise.forecasting.forecast_cache import (
    make_cache_key,
    read_cached_result,
    sha256_file,
    write_cached_result,
)
from stockwise.forecasting.lightgbm_evaluation import evaluate_lightgbm
from stockwise.forecasting.mlflow_tracking import track_forecast_run
from stockwise.forecasting.xgboost_evaluation import evaluate_xgboost


def _write_json_atomic(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as json_file:
            json.dump(content, json_file, indent=2, ensure_ascii=False)
            json_file.write("\n")
            json_file.flush()
            os.fsync(json_file.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_m5_sales(
    sales_path: Path,
    state: str,
    category: str,
    series_limit: int | None,
) -> tuple[list[str], dict[str, list[int]], dict[str, Any]]:
    if series_limit is not None and series_limit < 1:
        raise ValueError("series_limit must be at least 1 when provided")
    try:
        lazy_sales = pl.scan_parquet(sales_path).filter(
            (pl.col("state_id") == state) & (pl.col("cat_id") == category)
        ).sort("id")
        if series_limit is not None:
            lazy_sales = lazy_sales.head(series_limit)
        sales_frame = lazy_sales.collect(engine="streaming")
    except Exception as error:
        raise ValueError(f"Could not read filtered sales from {sales_path}: {error}") from error

    day_columns = [name for name in sales_frame.columns if name.startswith("d_")]
    if sales_frame.height == 0:
        raise ValueError(f"No sales series found for state={state} and category={category}")
    if not day_columns:
        raise ValueError("Sales Parquet has no daily d_ columns")

    series_by_id: dict[str, list[int]] = {}
    for row in sales_frame.iter_rows():
        series_by_id[str(row[0])] = [int(value) for value in row[6:]]

    selection = {
        "state_id": state,
        "category_id": category,
        "series_count": sales_frame.height,
        "total_matching_series": sales_frame.height if series_limit is None else None,
        "selection_method": "all matching series sorted by id" if series_limit is None else
            f"first {series_limit} matching series sorted by id; deterministic and not random",
        "series_limit": series_limit,
    }
    if series_limit is not None:
        total_count = pl.scan_parquet(sales_path).filter(
            (pl.col("state_id") == state) & (pl.col("cat_id") == category)
        ).select(pl.len()).collect(engine="streaming").item()
        selection["total_matching_series"] = total_count

    return day_columns, series_by_id, selection


def _read_day_dates(calendar_path: Path, day_columns: list[str]) -> list[str]:
    try:
        calendar = pl.scan_parquet(calendar_path).select("d", "date").collect(engine="streaming")
    except Exception as error:
        raise ValueError(f"Could not read calendar Parquet at {calendar_path}: {error}") from error

    calendar = calendar.with_columns(
        pl.col("d").str.strip_prefix("d_").cast(pl.Int64).alias("day_number")
    ).sort("day_number")
    date_by_number = {
        int(day_number): str(day_date)
        for day_number, day_date in calendar.select("day_number", "date").iter_rows()
    }
    dates: list[str] = []
    for day_column in day_columns:
        day_number = int(day_column[2:])
        if day_number not in date_by_number:
            raise ValueError(f"calendar Parquet has no date for {day_column}")
        dates.append(date_by_number[day_number])
    return dates


def run_m5_baseline_evaluation(
    project_root: Path,
    *,
    state: str = "CA",
    category: str = "FOODS",
    initial_window: int = 365,
    horizon: int = 28,
    folds: int = 3,
    recent_window: int = 28,
    ets_alpha: float = 0.2,
    croston_alpha: float = 0.1,
    series_limit: int | None = None,
    include_lightgbm: bool = False,
    include_xgboost: bool = False,
    include_chronos2: bool = False,
    include_timesfm25: bool = False,
    foundation_device: str = "cpu",
    foundation_batch_size: int = 8,
    tree_max_samples_per_series: int | None = None,
    use_cache: bool = True,
    track_mlflow: bool = False,
) -> Path:
    """Evaluate both baselines and write a JSON record under data/processed."""
    project_root = Path(project_root)
    m5_directory = project_root / "data" / "processed" / "m5"
    latest_path = m5_directory / "latest.json"
    if not latest_path.is_file():
        raise ValueError(f"M5 ingestion pointer not found: {latest_path}")
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        ingestion_report_path = m5_directory / latest["quality_report"]
        ingestion_report = json.loads(ingestion_report_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read the latest M5 ingestion report: {error}") from error
    if ingestion_report.get("status") != "passed":
        raise ValueError("Latest M5 ingestion report is not marked passed")

    run_directory = m5_directory / latest["run_directory"]
    sales_path = run_directory / "sales_train_validation.parquet"
    calendar_path = run_directory / "calendar.parquet"
    cache_directory = project_root / "data" / "processed" / "forecasts" / "cache"
    cache_key: str | None = None
    if use_cache:
        forecasting_source = Path(__file__).resolve().parent
        implementation_names = (
            "baselines.py",
            "evaluate_m5.py",
            "features.py",
            "foundation_models.py",
            "interval_metrics.py",
            "lightgbm_evaluation.py",
            "lightgbm_model.py",
            "xgboost_evaluation.py",
            "xgboost_model.py",
        )
        cache_configuration = {
            "inputs": {
                "sales_sha256": sha256_file(sales_path),
                "calendar_sha256": sha256_file(calendar_path),
            },
            "implementation_sha256": {
                name: sha256_file(forecasting_source / name) for name in implementation_names
            },
            "parameters": {
                "state": state,
                "category": category,
                "initial_window": initial_window,
                "horizon": horizon,
                "folds": folds,
                "recent_window": recent_window,
                "ets_alpha": ets_alpha,
                "croston_alpha": croston_alpha,
                "series_limit": series_limit,
                "include_lightgbm": include_lightgbm,
                "include_xgboost": include_xgboost,
                "include_chronos2": include_chronos2,
                "include_timesfm25": include_timesfm25,
                "foundation_device": foundation_device,
                "foundation_batch_size": foundation_batch_size,
                "tree_max_samples_per_series": tree_max_samples_per_series,
                "track_mlflow": track_mlflow,
            },
        }
        cache_key = make_cache_key(cache_configuration)
        cached_result = read_cached_result(cache_directory, cache_key)
        if cached_result is not None:
            previous_run_id = cached_result.get("run_id")
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
            cached_result["run_id"] = run_id
            cached_result["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
            cached_result["cache"] = {
                "status": "hit",
                "cache_key": cache_key,
                "reused_from_run_id": previous_run_id,
            }
            cached_path = project_root / "data" / "processed" / "forecasts" / f"baseline_{run_id}.json"
            _write_json_atomic(cached_path, cached_result)
            return cached_path

    day_columns, series_by_id, selection = _read_m5_sales(
        sales_path, state, category, series_limit
    )
    day_dates = _read_day_dates(calendar_path, day_columns)
    evaluation = evaluate_baselines(
        series_by_id,
        initial_window=initial_window,
        horizon=horizon,
        folds=folds,
        recent_window=recent_window,
        season_length=7,
        ets_alpha=ets_alpha,
        croston_alpha=croston_alpha,
    )

    optional_models: dict[str, Any] = {}
    if include_lightgbm:
        if series_limit is None:
            optional_models["lightgbm"] = {
                "status": "skipped",
                "reason": "LightGBM proof of concept requires an explicit --series-limit.",
            }
        elif series_limit > 100 and tree_max_samples_per_series is None:
            optional_models["lightgbm"] = {
                "status": "skipped",
                "reason": (
                    "For more than 100 series, set --tree-max-samples-per-series to cap "
                    "global training memory while still evaluating every selected series."
                ),
            }
        else:
            try:
                lightgbm_result = evaluate_lightgbm(
                    series_by_id,
                    initial_window=initial_window,
                    horizon=horizon,
                    folds=folds,
                    max_training_samples_per_series=tree_max_samples_per_series,
                )
                for fold in lightgbm_result["rolling_origin"]["folds"]:
                    start = fold["test_start_index"]
                    end = fold["test_end_index_exclusive"] - 1
                    fold["train_end_date"] = day_dates[start - 1]
                    fold["test_start_date"] = day_dates[start]
                    fold["test_end_date"] = day_dates[end]
                holdout = lightgbm_result["held_out"]
                holdout["train_end_date"] = day_dates[holdout["test_start_index"] - 1]
                holdout["test_start_date"] = day_dates[holdout["test_start_index"]]
                holdout["test_end_date"] = day_dates[holdout["test_end_index_exclusive"] - 1]
                optional_models["lightgbm"] = {
                    "status": "completed",
                    "evaluation": lightgbm_result,
                }
            except Exception as error:
                optional_models["lightgbm"] = {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                }

    if include_xgboost:
        if series_limit is None:
            optional_models["xgboost"] = {
                "status": "skipped",
                "reason": "XGBoost proof of concept requires an explicit --series-limit.",
            }
        elif series_limit > 100 and tree_max_samples_per_series is None:
            optional_models["xgboost"] = {
                "status": "skipped",
                "reason": (
                    "For more than 100 series, set --tree-max-samples-per-series to cap "
                    "global training memory while still evaluating every selected series."
                ),
            }
        else:
            try:
                xgboost_result = evaluate_xgboost(
                    series_by_id,
                    initial_window=initial_window,
                    horizon=horizon,
                    folds=folds,
                    max_training_samples_per_series=tree_max_samples_per_series,
                )
                for fold in xgboost_result["rolling_origin"]["folds"]:
                    start = fold["test_start_index"]
                    end = fold["test_end_index_exclusive"] - 1
                    fold["train_end_date"] = day_dates[start - 1]
                    fold["test_start_date"] = day_dates[start]
                    fold["test_end_date"] = day_dates[end]
                holdout = xgboost_result["held_out"]
                holdout["train_end_date"] = day_dates[holdout["test_start_index"] - 1]
                holdout["test_start_date"] = day_dates[holdout["test_start_index"]]
                holdout["test_end_date"] = day_dates[holdout["test_end_index_exclusive"] - 1]
                optional_models["xgboost"] = {
                    "status": "completed",
                    "evaluation": xgboost_result,
                }
            except Exception as error:
                optional_models["xgboost"] = {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                }

    foundation_candidates = (
        (
            "chronos2",
            include_chronos2,
            lambda: load_chronos2(device=foundation_device),
            forecast_chronos2,
        ),
        (
            "timesfm25",
            include_timesfm25,
            lambda: load_timesfm25(horizon=horizon, batch_size=foundation_batch_size),
            forecast_timesfm25,
        ),
    )
    for candidate_name, requested, loader, forecaster in foundation_candidates:
        if not requested:
            continue
        if series_limit is None:
            optional_models[candidate_name] = {
                "status": "skipped",
                "reason": (
                    "Foundation-model runs require an explicit --series-limit to make the "
                    "compute and weight-download choice deliberate. Use the exact matching "
                    "series count for the complete selected state/category."
                ),
            }
            continue
        try:
            model = loader()
            native_result = evaluate_native_quantiles(
                model,
                series_by_id,
                model_name=(
                    "chronos2_zero_shot" if candidate_name == "chronos2"
                    else "timesfm_2p5_zero_shot"
                ),
                forecast_function=forecaster,
                initial_window=initial_window,
                horizon=horizon,
                folds=folds,
            )
            for fold in native_result["rolling_origin"]["folds"]:
                start = fold["test_start_index"]
                end = fold["test_end_index_exclusive"] - 1
                fold["train_end_date"] = day_dates[start - 1]
                fold["test_start_date"] = day_dates[start]
                fold["test_end_date"] = day_dates[end]
            heldout_native = native_result["held_out"]
            heldout_native["train_end_date"] = day_dates[heldout_native["test_start_index"] - 1]
            heldout_native["test_start_date"] = day_dates[heldout_native["test_start_index"]]
            heldout_native["test_end_date"] = day_dates[heldout_native["test_end_index_exclusive"] - 1]
            optional_models[candidate_name] = {
                "status": "completed",
                "evaluation": native_result,
            }
        except Exception as error:
            optional_models[candidate_name] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "message": str(error),
            }

    for fold in evaluation["rolling_origin"]["folds"]:
        start = fold["test_start_index"]
        end = fold["test_end_index_exclusive"] - 1
        fold["train_end_date"] = day_dates[start - 1]
        fold["test_start_date"] = day_dates[start]
        fold["test_end_date"] = day_dates[end]
    holdout = evaluation["held_out"]
    holdout["train_end_date"] = day_dates[holdout["test_start_index"] - 1]
    holdout["test_start_date"] = day_dates[holdout["test_start_index"]]
    holdout["test_end_date"] = day_dates[holdout["test_end_index_exclusive"] - 1]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "data_provenance": {
            "source": "observed public M5 sales history",
            "synthetic_data_included": False,
            "m5_ingestion_run_id": latest["run_id"],
            "source_sales_parquet": str(sales_path.relative_to(project_root)),
            "source_calendar_parquet": str(calendar_path.relative_to(project_root)),
        },
        "selection": selection,
        "day_count": len(day_columns),
        "first_day": day_dates[0],
        "last_day": day_dates[-1],
        "evaluation": evaluation,
        "optional_models": optional_models,
        "cache": {
            "status": "miss" if use_cache else "disabled",
            "cache_key": cache_key,
        },
        "not_evaluated": {
            "direct_quantile_forecasts": (
                "models produce point forecasts; reported intervals are calibrated from earlier "
                "out-of-fold residuals, not direct quantile model outputs"
            ),
            "wrmsse": "hierarchical scaling and validated M5 aggregation weights are not yet calculated",
        },
        "mlflow_tracking": {"status": "not_requested"},
        "limitations": [
            "This comparison covers a single state/category subset and does not establish performance for all M5 series.",
            "Sales are observed units and may understate latent demand during stockouts.",
            "The deterministic first-N series option, when used, is not a random sample.",
            "Metrics are descriptive backtest results, not claims of retail savings or causal impact.",
        ],
    }
    output_directory = project_root / "data" / "processed" / "forecasts"
    result_path = output_directory / f"baseline_{run_id}.json"
    _write_json_atomic(result_path, result)
    if track_mlflow:
        try:
            track_forecast_run(
                result,
                result_path,
                project_root / "data" / "processed" / "mlflow",
            )
        except Exception as error:
            result["mlflow_tracking"] = {
                "status": "failed",
                "error_type": type(error).__name__,
                "message": str(error),
            }
            _write_json_atomic(result_path, result)
    if use_cache and cache_key is not None:
        try:
            write_cached_result(cache_directory, cache_key, result)
        except (OSError, TypeError, ValueError) as error:
            result["cache"] = {
                "status": "write_failed",
                "cache_key": cache_key,
                "error_type": type(error).__name__,
            }
            _write_json_atomic(result_path, result)
    return result_path


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Compare simple demand baselines with chronological M5 backtesting."
    )
    parser.add_argument("--state", default="CA", help="M5 state_id, default: CA")
    parser.add_argument("--category", default="FOODS", help="M5 cat_id, default: FOODS")
    parser.add_argument("--initial-window", type=int, default=365)
    parser.add_argument("--horizon", type=int, default=28)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--recent-window", type=int, default=28)
    parser.add_argument("--ets-alpha", type=float, default=0.2)
    parser.add_argument("--croston-alpha", type=float, default=0.1)
    parser.add_argument("--series-limit", type=int, default=None,
                        help="optional deterministic first-N subset for a faster smoke run")
    parser.add_argument("--include-lightgbm", action="store_true",
                        help="also evaluate the optional LightGBM model (requires --series-limit)")
    parser.add_argument("--include-xgboost", action="store_true",
                        help="also evaluate the optional XGBoost model (requires --series-limit)")
    parser.add_argument("--include-chronos2", action="store_true",
                        help="evaluate optional Chronos-2 native quantiles; requires explicit --series-limit")
    parser.add_argument("--include-timesfm25", action="store_true",
                        help="evaluate optional TimesFM 2.5 native quantiles; requires explicit --series-limit")
    parser.add_argument("--tree-max-samples-per-series", type=int, default=None,
                        help="deterministic time-spread training cap for global trees (required above 100 series)")
    parser.add_argument("--foundation-device", choices=("cpu", "cuda", "mps"), default="cpu",
                        help="Chronos-2 inference device; default: cpu")
    parser.add_argument("--foundation-batch-size", type=int, default=8,
                        help="TimesFM 2.5 inference batch size; reduce if memory is limited")
    parser.add_argument("--track-mlflow", action="store_true",
                        help="log this run's parameters, metrics, and JSON artifact to local MLflow")
    parser.add_argument("--no-cache", action="store_true",
                        help="always recompute instead of reusing an identical completed run")
    args = parser.parse_args()
    try:
        result_path = run_m5_baseline_evaluation(
            project_root,
            state=args.state,
            category=args.category,
            initial_window=args.initial_window,
            horizon=args.horizon,
            folds=args.folds,
            recent_window=args.recent_window,
            ets_alpha=args.ets_alpha,
            croston_alpha=args.croston_alpha,
            series_limit=args.series_limit,
            include_lightgbm=args.include_lightgbm,
            include_xgboost=args.include_xgboost,
            include_chronos2=args.include_chronos2,
            include_timesfm25=args.include_timesfm25,
            foundation_device=args.foundation_device,
            foundation_batch_size=args.foundation_batch_size,
            tree_max_samples_per_series=args.tree_max_samples_per_series,
            use_cache=not args.no_cache,
            track_mlflow=args.track_mlflow,
        )
    except (OSError, ValueError) as error:
        print(f"Baseline evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"Baseline evaluation completed. JSON run record: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("mlflow_tracking", {}).get("status") == "failed":
        print(
            "Forecast JSON was saved, but MLflow tracking failed: "
            f"{result['mlflow_tracking'].get('message', 'unknown error')}",
            file=sys.stderr,
        )
        return 1
    if result.get("mlflow_tracking", {}).get("status") == "completed":
        print(f"MLflow run: {result['mlflow_tracking']['mlflow_run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
