"""Validate the local M5 inputs and publish immutable Parquet runs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl


def _sales_headers() -> list[str]:
    return ["id", "item_id", "dept_id", "cat_id", "store_id", "state_id"] + [
        f"d_{day_number}" for day_number in range(1, 1914)
    ]


REQUIRED_HEADERS = {
    "calendar.csv": [
        "date", "wm_yr_wk", "weekday", "wday", "month", "year",
        "event_name_1", "event_type_1", "event_name_2", "event_type_2",
        "snap_CA", "snap_TX", "snap_WI", "d",
    ],
    "sales_train_validation.csv": _sales_headers(),
    "sell_prices.csv": ["store_id", "item_id", "wm_yr_wk", "sell_price"],
}

OPTIONAL_CALENDAR_COLUMNS = {
    "event_name_1", "event_type_1", "event_name_2", "event_type_2",
}
SALES_ID_COLUMNS = ["id", "item_id", "dept_id", "cat_id", "store_id", "state_id"]
PRICE_KEY_COLUMNS = ["store_id", "item_id", "wm_yr_wk"]
SAMPLE_ROW_LIMIT = 1000


class M5InputValidationError(ValueError):
    """Raised when an M5 file is missing or fails a required data check."""


class M5ProcessingError(RuntimeError):
    """Raised when validation, conversion, or publication cannot finish."""


def validate_headers(input_directory: Path) -> dict[str, list[str]]:
    """Check the required M5 filenames and CSV headers without loading rows."""
    input_directory = Path(input_directory)
    errors: list[str] = []
    observed_headers: dict[str, list[str]] = {}

    for file_name, required_headers in REQUIRED_HEADERS.items():
        file_path = input_directory / file_name
        if not file_path.is_file():
            errors.append(f"{file_name}: file not found at {file_path}")
            continue

        try:
            with file_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
                headers = next(csv.reader(csv_file), [])
        except (OSError, UnicodeError, csv.Error) as error:
            errors.append(f"{file_name}: could not read CSV header: {error}")
            continue

        if not headers:
            errors.append(f"{file_name}: empty file or missing header row")
            continue

        duplicate_headers = sorted({name for name in headers if headers.count(name) > 1})
        missing_headers = [name for name in required_headers if name not in headers]
        if duplicate_headers:
            errors.append(f"{file_name}: duplicate header columns: {', '.join(duplicate_headers)}")
        if missing_headers:
            errors.append(f"{file_name}: missing required columns: {', '.join(missing_headers)}")
        observed_headers[file_name] = headers

    if errors:
        raise M5InputValidationError("\n".join(errors))
    return observed_headers


def _collect(lazy_frame: pl.LazyFrame, description: str) -> pl.DataFrame:
    try:
        return lazy_frame.collect(engine="streaming")
    except Exception as error:
        raise M5InputValidationError(f"Could not read or parse {description}: {error}") from error


def _lazy_csv(input_directory: Path, file_name: str) -> pl.LazyFrame:
    if file_name == "calendar.csv":
        schema_overrides = {
            name: pl.String for name in REQUIRED_HEADERS[file_name]
            if name in {"date", "weekday", "event_name_1", "event_type_1", "event_name_2", "event_type_2", "d"}
        }
        schema_overrides.update({
            name: pl.Int64 for name in REQUIRED_HEADERS[file_name] if name not in schema_overrides
        })
    elif file_name == "sales_train_validation.csv":
        schema_overrides = {name: pl.String for name in SALES_ID_COLUMNS}
        schema_overrides.update({name: pl.Int64 for name in REQUIRED_HEADERS[file_name][6:]})
    else:
        schema_overrides = {
            "store_id": pl.String, "item_id": pl.String,
            "wm_yr_wk": pl.Int64, "sell_price": pl.Float64,
        }
    return pl.scan_csv(
        input_directory / file_name,
        schema_overrides=schema_overrides,
        try_parse_dates=False,
        raise_if_empty=True,
    )


def _sample_profile(
    lazy_frame: pl.LazyFrame,
    file_name: str,
    row_limit: int,
) -> dict[str, Any]:
    """Describe the first N source rows; this is a bounded, non-random sample."""
    sample = _collect(lazy_frame.head(row_limit), f"sample from {file_name}")
    profile: dict[str, Any] = {
        "rows_examined": sample.height,
        "method": f"first {row_limit} source rows; deterministic and not random",
        "null_cells": sum(sample.null_count().row(0)) if sample.height else 0,
    }
    if file_name == "sales_train_validation.csv" and sample.height:
        day_columns = [name for name in sample.columns if name.startswith("d_")]
        day_values = sample.select(pl.min_horizontal(day_columns).alias("min"),
                                   pl.max_horizontal(day_columns).alias("max"))
        profile["daily_units_min"] = day_values.get_column("min").min()
        profile["daily_units_max"] = day_values.get_column("max").max()
    elif file_name == "sell_prices.csv" and sample.height:
        profile["sell_price_min"] = sample.get_column("sell_price").min()
        profile["sell_price_max"] = sample.get_column("sell_price").max()
    elif file_name == "calendar.csv" and sample.height:
        profile["first_date"] = sample.get_column("date").min()
        profile["last_date"] = sample.get_column("date").max()
    return profile


def _source_provenance(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _table_summary(
    lazy_frame: pl.LazyFrame,
    file_name: str,
) -> tuple[dict[str, Any], list[str]]:
    try:
        schema = lazy_frame.collect_schema()
    except Exception as error:
        raise M5InputValidationError(f"{file_name}: could not determine CSV data types: {error}") from error
    columns = list(schema.names())
    errors: list[str] = []
    null_expressions = [pl.col(name).null_count().alias(f"null__{name}") for name in columns]
    expressions: list[pl.Expr] = [pl.len().alias("row_count"), *null_expressions]

    if file_name == "calendar.csv":
        expressions.extend([
            pl.col("d").n_unique().alias("unique_d"),
            pl.col("date").n_unique().alias("unique_date"),
            pl.col("date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
                .null_count().alias("invalid_date_count"),
            ((pl.col("date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False).dt.year()
              != pl.col("year"))
             | (pl.col("date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False).dt.month()
                != pl.col("month"))).fill_null(False).sum().alias("bad_date_parts"),
            (pl.col("date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False).dt.strftime("%A")
             != pl.col("weekday")).fill_null(False).sum().alias("bad_weekday_names"),
            pl.col("wm_yr_wk").n_unique().alias("calendar_week_count"),
            pl.col("d").str.strip_prefix("d_").cast(pl.Int64, strict=False)
                .min().alias("d_min"),
            pl.col("d").str.strip_prefix("d_").cast(pl.Int64, strict=False)
                .max().alias("d_max"),
            ((pl.col("wday") < 1) | (pl.col("wday") > 7)).sum().alias("bad_wday"),
            ((pl.col("month") < 1) | (pl.col("month") > 12)).sum().alias("bad_month"),
            pl.any_horizontal([
                ~pl.col(name).is_in([0, 1]) for name in ["snap_CA", "snap_TX", "snap_WI"]
            ]).sum().alias("bad_snap_rows"),
        ])
        expected_types = {
            "date": pl.String, "weekday": pl.String, "d": pl.String,
            "wm_yr_wk": pl.Int64, "wday": pl.Int64, "month": pl.Int64,
            "year": pl.Int64, "snap_CA": pl.Int64, "snap_TX": pl.Int64,
            "snap_WI": pl.Int64,
        }
        expected_types.update({name: pl.String for name in OPTIONAL_CALENDAR_COLUMNS})
    elif file_name == "sales_train_validation.csv":
        day_columns = [name for name in columns if name.startswith("d_")]
        expressions.extend([
            pl.col("id").n_unique().alias("unique_id"),
            pl.struct(["item_id", "store_id"]).n_unique().alias("unique_item_store"),
            pl.struct(["store_id", "state_id"]).n_unique().alias("unique_store_state"),
            pl.col("store_id").n_unique().alias("unique_store"),
            pl.struct(["item_id", "dept_id", "cat_id"]).n_unique().alias("unique_item_category"),
            pl.col("item_id").n_unique().alias("unique_item"),
            pl.min_horizontal([pl.col(name) for name in day_columns]).min().alias("minimum_units"),
            pl.any_horizontal([(pl.col(name) < 0).fill_null(False) for name in day_columns])
                .sum().alias("negative_sales_rows"),
        ])
        expected_types = {name: pl.String for name in SALES_ID_COLUMNS}
        expected_types.update({name: pl.Int64 for name in day_columns})
    else:
        expressions.extend([
            pl.struct(PRICE_KEY_COLUMNS).n_unique().alias("unique_price_key"),
            pl.col("sell_price").min().alias("minimum_price"),
            ((pl.col("sell_price") <= 0) | ~pl.col("sell_price").is_finite()
             | pl.col("sell_price").is_null())
                .sum().alias("invalid_price_rows"),
        ])
        expected_types = {
            "store_id": pl.String, "item_id": pl.String,
            "wm_yr_wk": pl.Int64, "sell_price": pl.Float64,
        }

    for name, expected_type in expected_types.items():
        actual_type = schema.get(name)
        if actual_type == pl.Null:
            continue
        if actual_type != expected_type:
            errors.append(
                f"{file_name}: column {name} has type {actual_type}, "
                f"expected {expected_type}"
            )

    if errors:
        raise M5InputValidationError("\n".join(errors))

    stats = _collect(lazy_frame.select(expressions), f"full validation scan of {file_name}").row(
        0, named=True
    )
    row_count = int(stats["row_count"])
    if row_count == 0:
        errors.append(f"{file_name}: input contains no data rows")

    null_counts = {
        name: int(stats[f"null__{name}"])
        for name in columns
        if int(stats[f"null__{name}"]) > 0
    }
    allowed_null_columns = OPTIONAL_CALENDAR_COLUMNS if file_name == "calendar.csv" else set()
    for name, count in null_counts.items():
        if name not in allowed_null_columns:
            errors.append(f"{file_name}: {count} null values in required column {name}")

    if file_name == "calendar.csv":
        if stats["unique_d"] != row_count:
            errors.append("calendar.csv: duplicate day keys in column d")
        if stats["unique_date"] != row_count:
            errors.append("calendar.csv: duplicate dates in column date")
        if stats["invalid_date_count"]:
            errors.append(f"calendar.csv: {stats['invalid_date_count']} invalid date values")
        if stats["bad_date_parts"]:
            errors.append(f"calendar.csv: {stats['bad_date_parts']} rows have year/month inconsistent with date")
        if stats["bad_weekday_names"]:
            errors.append(f"calendar.csv: {stats['bad_weekday_names']} weekday labels do not match date")
        if stats["bad_wday"]:
            errors.append(f"calendar.csv: {stats['bad_wday']} rows have wday outside 1..7")
        if stats["bad_month"]:
            errors.append(f"calendar.csv: {stats['bad_month']} rows have month outside 1..12")
        if stats["bad_snap_rows"]:
            errors.append(f"calendar.csv: {stats['bad_snap_rows']} rows have SNAP values outside 0 or 1")
        if stats["d_min"] != 1 or stats["d_max"] != row_count:
            errors.append("calendar.csv: day keys must provide continuous coverage from d_1")
        parsed_dates = _collect(
            lazy_frame.select(
                pl.col("date").str.strptime(pl.Date, format="%Y-%m-%d", strict=False)
                    .alias("parsed_date")
            ),
            "calendar date continuity check",
        ).get_column("parsed_date").sort()
        date_steps = parsed_dates.diff().dt.total_days()
        date_gaps = sum(step is not None and step != 1 for step in date_steps.to_list())
        if date_gaps:
            errors.append(f"calendar.csv: {date_gaps} gaps found in daily date coverage")
    elif file_name == "sales_train_validation.csv":
        if stats["unique_id"] != row_count:
            errors.append("sales_train_validation.csv: duplicate values in id")
        if stats["unique_item_store"] != row_count:
            errors.append("sales_train_validation.csv: duplicate item_id/store_id keys")
        if stats["unique_store_state"] != stats["unique_store"]:
            errors.append("sales_train_validation.csv: a store_id maps to more than one state_id")
        if stats["unique_item_category"] != stats["unique_item"]:
            errors.append("sales_train_validation.csv: an item_id maps to conflicting department/category values")
        if stats["negative_sales_rows"]:
            errors.append(
                f"sales_train_validation.csv: {stats['negative_sales_rows']} rows contain negative daily sales"
            )
    else:
        if stats["unique_price_key"] != row_count:
            errors.append("sell_prices.csv: duplicate store_id/item_id/wm_yr_wk keys")
        if stats["invalid_price_rows"]:
            errors.append(f"sell_prices.csv: {stats['invalid_price_rows']} rows have non-positive or null prices")

    warnings: list[str] = []
    optional_nulls = {name: count for name, count in null_counts.items() if name in allowed_null_columns}
    if optional_nulls:
        warnings.append(f"calendar.csv: optional event fields contain nulls: {optional_nulls}")

    return {
        "rows": row_count,
        "columns": len(columns),
        "column_names": columns,
        "column_types": {name: str(dtype) for name, dtype in schema.items()},
        "null_counts": null_counts,
        "sample_profile": None,
    }, errors + warnings


def inspect_m5_inputs(input_directory: Path, sample_rows: int = SAMPLE_ROW_LIMIT) -> dict[str, Any]:
    """Validate complete inputs and return a JSON-ready quality report."""
    if sample_rows < 1:
        raise ValueError("sample_rows must be at least 1")
    input_directory = Path(input_directory)
    headers = validate_headers(input_directory)
    tables: dict[str, pl.LazyFrame] = {
        file_name: _lazy_csv(input_directory, file_name) for file_name in REQUIRED_HEADERS
    }
    files: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for file_name, lazy_frame in tables.items():
        file_summary, findings = _table_summary(lazy_frame, file_name)
        file_summary["sample_profile"] = _sample_profile(lazy_frame, file_name, sample_rows)
        file_summary["source_file"] = _source_provenance(input_directory / file_name)
        files[file_name] = file_summary
        for finding in findings:
            if "optional event fields contain nulls" in finding:
                warnings.append(finding)
            else:
                errors.append(finding)

    calendar = tables["calendar.csv"]
    sales = tables["sales_train_validation.csv"]
    prices = tables["sell_prices.csv"]
    calendar_days = calendar.select(
        pl.col("d").str.strip_prefix("d_").alias("day_number")
    )
    sales_day_numbers = [int(name[2:]) for name in headers["sales_train_validation.csv"] if name.startswith("d_")]
    if sales_day_numbers:
        highest_sales_day = max(sales_day_numbers)
        missing_sales_days = _collect(
            calendar_days.filter(pl.col("day_number").cast(pl.Int64, strict=False) <= highest_sales_day)
            .select(pl.len()), "calendar day coverage",
        ).item()
        if missing_sales_days != highest_sales_day:
            errors.append(
                f"calendar.csv: expected calendar coverage for d_1..d_{highest_sales_day}; "
                f"found {missing_sales_days} matching rows"
            )

    sales_pairs = sales.select("item_id", "store_id").unique()
    price_pairs = prices.select("item_id", "store_id").unique()
    unknown_price_pairs = _collect(
        price_pairs.join(sales_pairs, on=["item_id", "store_id"], how="anti").select(pl.len()),
        "price-to-sales relationship check",
    ).item()
    if unknown_price_pairs:
        errors.append(f"sell_prices.csv: {unknown_price_pairs} item/store pairs are absent from sales data")

    sales_without_price = _collect(
        sales_pairs.join(price_pairs, on=["item_id", "store_id"], how="anti").select(pl.len()),
        "sales-to-price relationship check",
    ).item()
    if sales_without_price:
        warnings.append(
            f"{sales_without_price} sales item/store pairs have no sell-price record; "
            "confirm whether this is expected for the supplied subset"
        )

    calendar_weeks = calendar.select("wm_yr_wk").unique()
    unknown_price_weeks = _collect(
        prices.select("wm_yr_wk").unique()
        .join(calendar_weeks, on="wm_yr_wk", how="anti").select(pl.len()),
        "price-week relationship check",
    ).item()
    if unknown_price_weeks:
        errors.append(f"sell_prices.csv: {unknown_price_weeks} price weeks are absent from calendar.csv")

    return {
        "schema_version": 1,
        "status": "failed" if errors else "passed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "source": "M5 Forecasting Accuracy competition files supplied locally",
            "source_directory": str(input_directory.resolve()),
            "observed_data": True,
            "synthetic_data_included": False,
        },
        "validation_scope": {
            "required_schema_types_keys_ranges_nulls_and_relationships": "full files",
            "sample_profile_rows_per_file": sample_rows,
            "sample_method": "first N source rows; deterministic, not random, and descriptive only",
        },
        "checks": {
            "required_headers": "passed",
            "full_file_types_nulls_keys_ranges_and_relationships": "failed" if errors else "passed",
            "sample_profiles": "descriptive_only",
        },
        "files": files,
        "errors": errors,
        "warnings": warnings,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as json_file:
            json.dump(payload, json_file, indent=2, ensure_ascii=False)
            json_file.write("\n")
            json_file.flush()
            os.fsync(json_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_ingestion(
    input_directory: Path,
    output_directory: Path,
    sample_rows: int = SAMPLE_ROW_LIMIT,
) -> Path:
    """Validate inputs and atomically publish one immutable processed-data run."""
    input_directory = Path(input_directory)
    output_directory = Path(output_directory)
    try:
        report = inspect_m5_inputs(input_directory, sample_rows)
    except Exception as error:
        raise M5ProcessingError(f"Input inspection failed: {error}") from error

    if report["errors"]:
        raise M5ProcessingError("Input validation failed:\n- " + "\n- ".join(report["errors"]))

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    stage_directory = output_directory / ".staging" / run_id
    runs_directory = output_directory / "runs"
    final_directory = runs_directory / run_id
    try:
        stage_directory.mkdir(parents=True, exist_ok=False)
        for file_name in REQUIRED_HEADERS:
            parquet_name = file_name.removesuffix(".csv") + ".parquet"
            try:
                _lazy_csv(input_directory, file_name).sink_parquet(
                    stage_directory / parquet_name,
                    compression="zstd",
                    engine="streaming",
                )
            except Exception as error:
                raise M5ProcessingError(f"Failed converting {file_name} to Parquet: {error}") from error

            parquet_frame = pl.scan_parquet(stage_directory / parquet_name)
            parquet_rows = _collect(parquet_frame.select(pl.len()), f"written {parquet_name}").item()
            if parquet_rows != report["files"][file_name]["rows"]:
                raise M5ProcessingError(
                    f"{parquet_name}: wrote {parquet_rows} rows, expected {report['files'][file_name]['rows']}"
                )
            report["files"][file_name]["parquet"] = {
                "file": parquet_name,
                "rows": parquet_rows,
                "bytes": (stage_directory / parquet_name).stat().st_size,
            }

        report["status"] = "passed"
        report["run_id"] = run_id
        report["outputs"] = [
            f"runs/{run_id}/{file_name.removesuffix('.csv')}.parquet" for file_name in REQUIRED_HEADERS
        ]
        report_path = stage_directory / "quality_report.json"
        _write_json_atomic(report_path, report)

        runs_directory.mkdir(parents=True, exist_ok=True)
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        stage_directory.rename(final_directory)
        latest_path = output_directory / "latest.json"
        _write_json_atomic(latest_path, {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "quality_report": f"runs/{run_id}/quality_report.json",
        })
        return final_directory / "quality_report.json"
    except Exception as error:
        if stage_directory.exists():
            shutil.rmtree(stage_directory, ignore_errors=True)
        if isinstance(error, M5ProcessingError):
            raise
        raise M5ProcessingError(f"M5 ingestion was not published: {error}") from error


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Validate and convert local M5 CSV files to Parquet.")
    parser.add_argument("--input-directory", type=Path, default=project_root / "data" / "raw")
    parser.add_argument("--output-directory", type=Path, default=project_root / "data" / "processed" / "m5")
    parser.add_argument("--sample-rows", type=int, default=SAMPLE_ROW_LIMIT)
    args = parser.parse_args()
    try:
        report_path = run_ingestion(args.input_directory, args.output_directory, args.sample_rows)
    except M5ProcessingError as error:
        print(f"M5 ingestion failed:\n{error}", file=sys.stderr)
        return 1
    print(f"M5 ingestion succeeded. Quality report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
