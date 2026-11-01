"""Connect the synthetic inventory generator to the latest M5 Parquet run."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from backend.simulation.inventory import SIMULATOR_VERSION, generate_simulated_inventory


class InventorySimulationError(ValueError):
    """Raised when the M5 inputs cannot safely produce a simulation run."""


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventorySimulationError(f"Could not read {description} at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise InventorySimulationError(f"{description} at {path} must contain a JSON object")
    return payload


def _resolve_within(path_root: Path, relative_path: str, description: str) -> Path:
    candidate = (path_root / relative_path).resolve()
    root = path_root.resolve()
    if not candidate.is_relative_to(root):
        raise InventorySimulationError(f"{description} path escapes its data directory")
    return candidate


def _load_latest_m5(project_root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    m5_directory = project_root / "data" / "processed" / "m5"
    latest = _read_json(m5_directory / "latest.json", "M5 latest pointer")
    try:
        run_directory = _resolve_within(m5_directory, latest["run_directory"], "M5 run directory")
        report_path = _resolve_within(m5_directory, latest["quality_report"], "M5 quality report")
        run_id = latest["run_id"]
    except (KeyError, TypeError) as error:
        raise InventorySimulationError(f"M5 latest pointer is missing required fields: {error}") from error

    report = _read_json(report_path, "M5 quality report")
    if report.get("status") != "passed" or report.get("run_id") != run_id:
        raise InventorySimulationError("M5 latest ingestion run is not a matching successful run")
    if not run_directory.is_dir():
        raise InventorySimulationError(f"M5 run directory does not exist: {run_directory}")
    return run_directory, latest, report


def _read_source_records(
    run_directory: Path,
    *,
    state: str,
    category: str,
    series_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if series_limit < 1:
        raise InventorySimulationError("series_limit must be at least 1")
    sales_path = run_directory / "sales_train_validation.parquet"
    calendar_path = run_directory / "calendar.parquet"
    prices_path = run_directory / "sell_prices.parquet"
    try:
        sales_scan = pl.scan_parquet(sales_path).filter(
            (pl.col("state_id") == state) & (pl.col("cat_id") == category)
        ).sort("id")
        total_matching_series = sales_scan.select(pl.len()).collect(engine="streaming").item()
        if not total_matching_series:
            raise InventorySimulationError(f"No M5 series found for state={state} and category={category}")
        sales_schema = sales_scan.collect_schema()
        day_columns = sorted(
            [name for name in sales_schema.names() if name.startswith("d_") and name[2:].isdigit()],
            key=lambda name: int(name[2:]),
        )
        if len(day_columns) < 1:
            raise InventorySimulationError("M5 sales Parquet contains no d_ day columns")
        selected_days = day_columns[-28:]
        selected_sales = sales_scan.head(series_limit).select(
            "id", "item_id", "store_id", "state_id", "cat_id", *selected_days
        ).collect(engine="streaming")
        last_day = day_columns[-1]
        calendar = pl.scan_parquet(calendar_path).select("d", "date", "wm_yr_wk").collect(engine="streaming")
        day_row = calendar.filter(pl.col("d") == last_day)
        if day_row.height != 1:
            raise InventorySimulationError(f"M5 calendar must contain exactly one row for {last_day}")
        calendar_values = day_row.row(0, named=True)
        simulation_date = date.fromisoformat(str(calendar_values["date"]))
        target_week = int(calendar_values["wm_yr_wk"])

        selected_pairs = selected_sales.select("item_id", "store_id").unique()
        matching_prices = (
            pl.scan_parquet(prices_path)
            .filter(pl.col("wm_yr_wk") == target_week)
            .join(selected_pairs.lazy(), on=["item_id", "store_id"], how="inner")
            .select("item_id", "store_id", "sell_price")
            .collect(engine="streaming")
        )
    except InventorySimulationError:
        raise
    except Exception as error:
        raise InventorySimulationError(f"Could not read M5 Parquet inputs: {error}") from error

    if selected_sales.height != min(series_limit, total_matching_series):
        raise InventorySimulationError("M5 sales subset row count does not match the requested selection")
    columns_with_nulls = [
        name for name in selected_sales.columns if selected_sales[name].null_count() > 0
    ]
    if columns_with_nulls:
        raise InventorySimulationError(
            f"Selected M5 sales subset contains null values in: {columns_with_nulls}"
        )

    price_by_pair: dict[tuple[str, str], float] = {}
    for row in matching_prices.iter_rows(named=True):
        key = (str(row["item_id"]), str(row["store_id"]))
        if key in price_by_pair:
            raise InventorySimulationError(f"Duplicate M5 price for item/store at week {target_week}: {key}")
        price = float(row["sell_price"])
        if price <= 0:
            raise InventorySimulationError(f"Invalid non-positive M5 selling price for {key}: {price}")
        price_by_pair[key] = price

    source_records: list[dict[str, Any]] = []
    for row in selected_sales.iter_rows(named=True):
        pair = (str(row["item_id"]), str(row["store_id"]))
        if pair not in price_by_pair:
            raise InventorySimulationError(
                f"No observed M5 selling price for item/store {pair} in week {target_week}"
            )
        source_records.append({
            "series_id": str(row["id"]),
            "item_id": str(row["item_id"]),
            "store_id": str(row["store_id"]),
            "state_id": str(row["state_id"]),
            "category_id": str(row["cat_id"]),
            "recent_sales": [row[name] for name in selected_days],
            "observed_retail_price": price_by_pair[pair],
        })

    selection = {
        "state_id": state,
        "category_id": category,
        "series_count": len(source_records),
        "total_matching_series": total_matching_series,
        "selection_method": f"first {len(source_records)} matching series sorted by id; deterministic and not random",
        "series_limit": series_limit,
        "demand_reference_days": selected_days,
        "observed_price_week": target_week,
        "as_of_date": simulation_date.isoformat(),
    }
    return source_records, selection


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            json.dump(payload, output_file, indent=2, ensure_ascii=False)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_m5_inventory_simulation(
    project_root: Path,
    *,
    state: str = "CA",
    category: str = "FOODS",
    series_limit: int = 25,
    seed: int = 20261029,
) -> Path:
    """Generate and atomically publish simulated inventory for a bounded M5 subset."""
    project_root = Path(project_root).resolve()
    run_directory, latest_m5, _ = _load_latest_m5(project_root)
    source_records, selection = _read_source_records(
        run_directory, state=state, category=category, series_limit=series_limit
    )
    simulation_date = date.fromisoformat(selection["as_of_date"])
    records = generate_simulated_inventory(source_records, as_of_date=simulation_date, seed=seed)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "simulator": {
            "name": "backend.synthetic_inventory",
            "version": SIMULATOR_VERSION,
            "seed": seed,
        },
        "data_provenance": {
            "observed_source": "public M5 sales, calendar, and selling prices",
            "synthetic_operational_records": True,
            "m5_ingestion_run_id": latest_m5["run_id"],
            "source_sales_parquet": str((run_directory / "sales_train_validation.parquet").relative_to(project_root)),
            "source_calendar_parquet": str((run_directory / "calendar.parquet").relative_to(project_root)),
            "source_prices_parquet": str((run_directory / "sell_prices.parquet").relative_to(project_root)),
        },
        "selection": selection,
        "records": records,
        "limitations": [
            "Operational inventory, expiry, lead time, purchase orders, and unit cost are simulated assumptions.",
            "Recent observed M5 sales are a demand reference, not latent demand or a forecast.",
            "The deterministic first-N selection is not a random sample.",
        ],
    }

    output_directory = project_root / "data" / "processed" / "synthetic_inventory"
    run_id_directory = output_directory / "runs" / run_id
    staging_directory = output_directory / ".staging" / run_id
    try:
        staging_directory.mkdir(parents=True, exist_ok=False)
        _write_json_atomic(staging_directory / "inventory.json", payload)
        (output_directory / "runs").mkdir(parents=True, exist_ok=True)
        staging_directory.rename(run_id_directory)
        _write_json_atomic(output_directory / "latest.json", {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "records_file": f"runs/{run_id}/inventory.json",
        })
    except Exception as error:
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)
        if isinstance(error, InventorySimulationError):
            raise
        raise InventorySimulationError(f"Synthetic inventory run was not published: {error}") from error
    return run_id_directory / "inventory.json"


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Generate a bounded, seeded synthetic inventory set from the latest M5 Parquet run."
    )
    parser.add_argument("--state", default="CA", help="M5 state_id, default: CA")
    parser.add_argument("--category", default="FOODS", help="M5 cat_id, default: FOODS")
    parser.add_argument("--series-limit", type=int, default=25,
                        help="deterministic first-N subset limit; default: 25")
    parser.add_argument("--seed", type=int, default=20261029,
                        help="deterministic simulator seed; default: 20261029")
    args = parser.parse_args()
    try:
        result_path = run_m5_inventory_simulation(
            project_root,
            state=args.state,
            category=args.category,
            series_limit=args.series_limit,
            seed=args.seed,
        )
    except (OSError, ValueError) as error:
        print(f"Synthetic inventory simulation failed: {error}", file=sys.stderr)
        return 1
    print(f"Synthetic inventory simulation completed. Run record: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
