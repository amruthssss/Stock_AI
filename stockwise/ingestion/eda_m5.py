"""Create a descriptive EDA bundle from a validated local M5 Parquet run."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SALES_FILE = "sales_train_validation.parquet"
CALENDAR_FILE = "calendar.parquet"
PRICE_FILE = "sell_prices.parquet"
DAY_PATTERN = re.compile(r"^d_(\d+)$")


class M5EDAError(ValueError):
    """Raised when the validated Parquet inputs cannot support the EDA run."""


def _read_latest_ingestion_run(processed_root: Path) -> tuple[Path, str]:
    pointer_path = processed_root / "latest.json"
    if not pointer_path.is_file():
        raise M5EDAError(f"M5 latest-run pointer not found: {pointer_path}")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        run_id = str(pointer["run_id"])
        relative_run = Path(pointer["run_directory"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise M5EDAError(f"Invalid M5 latest-run pointer: {error}") from error
    if relative_run.is_absolute() or ".." in relative_run.parts:
        raise M5EDAError("M5 latest-run pointer contains an unsafe run directory")
    run_directory = processed_root / relative_run
    if not run_directory.is_dir():
        raise M5EDAError(f"M5 ingestion run directory not found: {run_directory}")
    return run_directory, run_id


def _day_columns(columns: list[str]) -> list[str]:
    matches = [(int(match.group(1)), name) for name in columns
               if (match := DAY_PATTERN.fullmatch(name))]
    matches.sort()
    if not matches or [number for number, _ in matches] != list(range(1, len(matches) + 1)):
        raise M5EDAError("Sales Parquet must contain a continuous sequence of columns d_1, d_2, ...")
    return [name for _, name in matches]


def _check_inputs(run_directory: Path) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, list[str]]:
    paths = {
        "sales": run_directory / SALES_FILE,
        "calendar": run_directory / CALENDAR_FILE,
        "prices": run_directory / PRICE_FILE,
    }
    for name, path in paths.items():
        if not path.is_file():
            raise M5EDAError(f"{name} Parquet file not found: {path}")

    try:
        sales = pl.read_parquet(paths["sales"])
        calendar = pl.read_parquet(paths["calendar"])
        prices = pl.read_parquet(paths["prices"])
    except Exception as error:
        raise M5EDAError(f"Could not read an M5 Parquet input: {error}") from error

    required_sales = {"id", "item_id", "dept_id", "cat_id", "store_id", "state_id"}
    required_calendar = {"d", "date", "weekday", "wday", "month", "year"}
    required_prices = {"store_id", "item_id", "wm_yr_wk", "sell_price"}
    for label, frame, required in (
        ("sales", sales, required_sales),
        ("calendar", calendar, required_calendar),
        ("prices", prices, required_prices),
    ):
        missing = sorted(required - set(frame.columns))
        if missing:
            raise M5EDAError(f"{label} Parquet is missing required columns: {', '.join(missing)}")
        if frame.is_empty():
            raise M5EDAError(f"{label} Parquet contains no rows")

    day_columns = _day_columns(sales.columns)
    invalid_day_values = sales.select(
        pl.any_horizontal([
            pl.col(name).is_null() | (pl.col(name) < 0) | ~pl.col(name).is_finite()
            for name in day_columns
        ]).sum().alias("invalid_rows")
    ).item()
    if invalid_day_values:
        raise M5EDAError(f"Sales Parquet has invalid values in {invalid_day_values} rows")
    invalid_prices = prices.select(
        ((pl.col("sell_price") <= 0) | ~pl.col("sell_price").is_finite()
         | pl.col("sell_price").is_null()).sum().alias("invalid_rows")
    ).item()
    if invalid_prices:
        raise M5EDAError(f"Price Parquet has invalid values in {invalid_prices} rows")
    if calendar.get_column("d").n_unique() != calendar.height:
        raise M5EDAError("Calendar Parquet contains duplicate day keys")
    if calendar.get_column("date").n_unique() != calendar.height:
        raise M5EDAError("Calendar Parquet contains duplicate dates")
    if prices.select(pl.struct(["store_id", "item_id", "wm_yr_wk"]).n_unique()).item() != prices.height:
        raise M5EDAError("Price Parquet contains duplicate store/item/week keys")

    sales_day_set = set(day_columns)
    calendar_day_set = set(calendar.get_column("d").to_list())
    missing_days = sorted(sales_day_set - calendar_day_set)
    if missing_days:
        raise M5EDAError(f"Calendar Parquet does not cover sales days: {', '.join(missing_days[:5])}")
    return sales, calendar, prices, day_columns


def _read_quality_report(run_directory: Path) -> dict[str, Any]:
    quality_path = run_directory / "quality_report.json"
    try:
        quality_report = json.loads(quality_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise M5EDAError(f"Could not read ingestion quality report: {error}") from error
    if quality_report.get("status") != "passed":
        raise M5EDAError("EDA requires an ingestion quality report with status 'passed'")
    if quality_report.get("provenance", {}).get("synthetic_data_included") is not False:
        raise M5EDAError("EDA requires an ingestion report identifying the inputs as non-synthetic M5 data")
    return quality_report


def _daily_sales(sales: pl.DataFrame, calendar: pl.DataFrame, days: list[str]) -> pl.DataFrame:
    sums = sales.select([pl.col(day).sum().alias(day) for day in days]).row(0, named=True)
    totals = pl.DataFrame({"d": days, "total_units": [int(sums[day]) for day in days]})
    calendar_columns = [name for name in [
        "d", "date", "weekday", "wday", "month", "year", "snap_CA", "snap_TX", "snap_WI",
        "event_name_1", "event_type_1", "event_name_2", "event_type_2",
    ] if name in calendar.columns]
    return totals.join(calendar.select(calendar_columns), on="d", how="left").sort("date")


def _group_summaries(sales: pl.DataFrame, days: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
    enriched = sales.with_columns([
        pl.sum_horizontal([pl.col(day) for day in days]).alias("series_total_units"),
        pl.sum_horizontal([(pl.col(day) == 0).cast(pl.Int64) for day in days])
        .alias("series_zero_days"),
    ]).with_columns((pl.col("series_zero_days") / len(days)).alias("zero_day_fraction"))
    by_category_state = enriched.group_by(["state_id", "cat_id"]).agg([
        pl.len().alias("series_count"),
        pl.col("series_total_units").sum().alias("total_units"),
        pl.col("series_total_units").mean().round(2).alias("mean_units_per_series"),
        pl.col("zero_day_fraction").mean().round(4).alias("mean_zero_day_fraction"),
    ]).sort(["state_id", "cat_id"])
    by_state = enriched.group_by("state_id").agg([
        pl.len().alias("series_count"),
        pl.col("series_total_units").sum().alias("total_units"),
        pl.col("series_total_units").mean().round(2).alias("mean_units_per_series"),
        pl.col("zero_day_fraction").mean().round(4).alias("mean_zero_day_fraction"),
    ]).sort("state_id")
    return by_category_state, by_state


def _time_summaries(daily: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    weekday = daily.group_by(["wday", "weekday"]).agg([
        pl.len().alias("day_count"),
        pl.col("total_units").sum().alias("total_units"),
        pl.col("total_units").mean().round(2).alias("mean_daily_units"),
    ]).sort("wday")
    monthly = daily.group_by(["year", "month"]).agg([
        pl.len().alias("day_count"),
        pl.col("total_units").sum().alias("total_units"),
        pl.col("total_units").mean().round(2).alias("mean_daily_units"),
    ]).sort(["year", "month"])
    return weekday, monthly


def _svg_line(values: list[int], width: int = 900, height: int = 220) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    spread = max(high - low, 1)
    points = []
    for index, value in enumerate(values):
        x = index * (width - 12) / max(len(values) - 1, 1) + 6
        y = height - 12 - ((value - low) / spread) * (height - 24)
        points.append(f"{x:.1f},{y:.1f}")
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Daily total sales, {len(values)} days">'
            f'<polyline fill="none" stroke="#087e68" stroke-width="2" '
            f'points="{" ".join(points)}"/></svg>')


def _svg_bars(labels: list[str], values: list[float], width: int = 900, height: int = 250) -> str:
    if not values:
        return ""
    maximum = max(max(values), 1)
    slot = width / len(values)
    rects = []
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        bar_height = max(1, (height - 52) * value / maximum)
        x = index * slot + slot * 0.18
        y = height - 34 - bar_height
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{slot * .64:.1f}" '
            f'height="{bar_height:.1f}" rx="4" fill="#087e68"/>'
            f'<text x="{index * slot + slot / 2:.1f}" y="{height - 12}" '
            f'text-anchor="middle">{html.escape(label)}</text>'
        )
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(rects)}</svg>'


def _render_html(report: dict[str, Any], daily: pl.DataFrame,
                 by_category_state: pl.DataFrame, by_state: pl.DataFrame,
                 by_weekday: pl.DataFrame, by_month: pl.DataFrame) -> str:
    totals = daily.get_column("total_units").to_list()
    states = by_state.get_column("state_id").to_list()
    state_units = by_state.get_column("total_units").to_list()
    categories = by_category_state.with_columns(
        pl.concat_str([pl.col("state_id"), pl.lit(" / "), pl.col("cat_id")]).alias("label")
    )
    cat_labels = categories.get_column("label").to_list()
    cat_values = categories.get_column("total_units").to_list()
    weekday_labels = by_weekday.get_column("weekday").to_list()
    weekday_values = by_weekday.get_column("mean_daily_units").to_list()
    month_rows = "".join(
        f"<tr><td>{row['year']}</td><td>{row['month']}</td><td>{row['day_count']}</td>"
        f"<td>{row['total_units']:,}</td><td>{row['mean_daily_units']:,.2f}</td></tr>"
        for row in by_month.iter_rows(named=True)
    )
    counts = report["sales_summary"]
    provenance = report["provenance"]
    limitations = "".join(
        f"<li>{html.escape(item)}</li>" for item in report["analysis_scope"]["limitations"]
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>StockWise M5 sales exploration</title><style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:36px auto;padding:0 20px;color:#1c2c29;background:#f5f8f6}}
h1,h2{{line-height:1.2}}.eyebrow{{color:#087e68;letter-spacing:.13em;text-transform:uppercase;font-weight:700}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}article,section{{background:white;border:1px solid #dce7e2;border-radius:12px;padding:18px;margin:16px 0}}
.metric{{font-size:1.5rem;font-weight:700}}small,.muted{{color:#50645d}}svg{{width:100%;height:auto}}svg text{{font:12px system-ui,sans-serif;fill:#40544d}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-bottom:1px solid #e0e8e4;text-align:left}}
</style></head><body>
<p class="eyebrow">StockWise · observed public data</p><h1>M5 sales exploration</h1>
<p>This report describes the validated M5 sales history. Operational inventory, expiry, and purchase data are not present in M5; those are synthetic elsewhere in StockWise. No sales forecasts or causal conclusions are made here.</p>
<div class="cards"><article><div class="muted">Item/store series</div><div class="metric">{counts['series_count']:,}</div></article>
<article><div class="muted">Sales days</div><div class="metric">{counts['day_count']:,}</div></article>
<article><div class="muted">Observed units</div><div class="metric">{counts['total_units']:,}</div></article>
<article><div class="muted">Zero-valued observations</div><div class="metric">{counts['zero_sales_fraction']:.1%}</div></article>
<article><div class="muted">Median listed price</div><div class="metric">${report['price_summary']['median_price']:.2f}</div></article></div>
<section><h2>Daily total sales</h2><p class="muted">All listed item/store series summed per calendar day. The chart uses every sales day in this M5 run.</p>{_svg_line(totals)}</section>
<section><h2>Average daily sales by weekday</h2><p class="muted">This is a descriptive average across observed dates; it is not a seasonal forecast.</p>{_svg_bars(weekday_labels, weekday_values)}</section>
<section><h2>Total observed units by state</h2>{_svg_bars(states, state_units)}</section>
<section><h2>Total observed units by state and category</h2>{_svg_bars(cat_labels, cat_values)}</section>
<section><h2>Monthly summary</h2><p class="muted">Some boundary months are partial because the M5 sales history covers a fixed date range.</p><table><thead><tr><th>Year</th><th>Month</th><th>Observed days</th><th>Total units</th><th>Mean daily units</th></tr></thead><tbody>{month_rows}</tbody></table></section>
<section><h2>Source and scope</h2><table><tbody>
<tr><th>Ingestion run</th><td>{html.escape(str(provenance['ingestion_run_id']))}</td></tr>
<tr><th>Source</th><td>{html.escape(str(provenance['source']))}</td></tr>
<tr><th>Data scope</th><td>Full validated Parquet input; no sampling in this EDA run.</td></tr>
<tr><th>Price records</th><td>{report['price_summary']['row_count']:,}; price analysis is descriptive and does not infer margin or inventory.</td></tr>
</tbody></table><h3>Interpretation limits</h3><ul>{limitations}</ul></section>
<p class="muted">Generated at {html.escape(str(report['generated_at_utc']))}. For exact tabular values, use the CSV files in this report bundle.</p>
</body></html>"""


def _write_bundle(staging: Path, report: dict[str, Any], daily: pl.DataFrame,
                  by_category_state: pl.DataFrame, by_state: pl.DataFrame,
                  by_store_price: pl.DataFrame, by_weekday: pl.DataFrame,
                  by_month: pl.DataFrame) -> None:
    staging.mkdir(parents=True, exist_ok=False)
    daily.write_csv(staging / "daily_sales.csv")
    by_category_state.write_csv(staging / "sales_by_state_category.csv")
    by_state.write_csv(staging / "sales_by_state.csv")
    by_store_price.write_csv(staging / "prices_by_store.csv")
    by_weekday.write_csv(staging / "sales_by_weekday.csv")
    by_month.write_csv(staging / "sales_by_month.csv")
    (staging / "eda_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (staging / "index.html").write_text(
        _render_html(report, daily, by_category_state, by_state, by_weekday, by_month), encoding="utf-8"
    )


def _publish_pointer(output_directory: Path, pointer: dict[str, str]) -> None:
    temporary_path = output_directory / f".latest-{uuid.uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8") as pointer_file:
            json.dump(pointer, pointer_file, indent=2)
            pointer_file.write("\n")
            pointer_file.flush()
            os.fsync(pointer_file.fileno())
        os.replace(temporary_path, output_directory / "latest.json")
    finally:
        temporary_path.unlink(missing_ok=True)


def run_m5_eda(
    ingestion_run_directory: Path,
    output_directory: Path,
    *,
    ingestion_run_id: str,
) -> Path:
    """Create a separate, immutable EDA run and atomically update its pointer."""
    ingestion_run_directory = Path(ingestion_run_directory)
    output_directory = Path(output_directory)
    quality_report = _read_quality_report(ingestion_run_directory)
    sales, calendar, prices, days = _check_inputs(ingestion_run_directory)
    daily = _daily_sales(sales, calendar, days)
    by_category_state, by_state = _group_summaries(sales, days)
    by_weekday, by_month = _time_summaries(daily)
    zero_observations = sales.select(
        pl.sum_horizontal([(pl.col(day) == 0).cast(pl.Int64) for day in days]).sum().alias("zeros")
    ).item()
    total_observations = sales.height * len(days)
    total_units = int(daily.get_column("total_units").sum())
    price_by_store = prices.group_by("store_id").agg([
        pl.len().alias("price_records"),
        pl.col("sell_price").min().round(4).alias("minimum_price"),
        pl.col("sell_price").median().round(4).alias("median_price"),
        pl.col("sell_price").mean().round(4).alias("mean_price"),
        pl.col("sell_price").max().round(4).alias("maximum_price"),
    ]).sort("store_id")

    price_stats = prices.select([
        pl.len().alias("row_count"),
        pl.col("store_id").n_unique().alias("store_count"),
        pl.col("item_id").n_unique().alias("item_count"),
        pl.col("wm_yr_wk").n_unique().alias("week_count"),
        pl.col("sell_price").min().alias("minimum_price"),
        pl.col("sell_price").median().alias("median_price"),
        pl.col("sell_price").mean().alias("mean_price"),
        pl.col("sell_price").max().alias("maximum_price"),
    ]).row(0, named=True)
    daily_values = daily.get_column("total_units")
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "source": quality_report.get("provenance", {}).get("source", "local M5 Parquet"),
            "ingestion_run_id": ingestion_run_id,
            "observed_data": True,
            "synthetic_data_included": False,
        },
        "analysis_scope": {
            "sales_rows": "full file",
            "sales_days": "all contiguous d_1..d_N columns present in the validated Parquet",
            "price_rows": "full file",
            "sampled": False,
            "limitations": [
                "Descriptive associations do not establish causes or forecast accuracy.",
                "M5 sales do not contain on-hand inventory, expiry, or purchase orders.",
                "Zero sales can reflect no demand or a stockout; this data cannot distinguish them.",
            ],
        },
        "sales_summary": {
            "series_count": sales.height,
            "day_count": len(days),
            "total_observations": total_observations,
            "total_units": total_units,
            "mean_daily_total_units": round(float(daily_values.mean()), 2),
            "minimum_daily_total_units": int(daily_values.min()),
            "maximum_daily_total_units": int(daily_values.max()),
            "zero_sales_observations": int(zero_observations),
            "zero_sales_fraction": round(zero_observations / total_observations, 6),
            "state_count": sales.get_column("state_id").n_unique(),
            "category_count": sales.get_column("cat_id").n_unique(),
        },
        "price_summary": {key: (round(float(value), 4) if isinstance(value, float) else int(value))
                          for key, value in price_stats.items()},
        "files": {
            "daily_sales": "daily_sales.csv",
            "sales_by_state_category": "sales_by_state_category.csv",
            "sales_by_state": "sales_by_state.csv",
            "prices_by_store": "prices_by_store.csv",
            "sales_by_weekday": "sales_by_weekday.csv",
            "sales_by_month": "sales_by_month.csv",
            "html_report": "index.html",
        },
    }

    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "runs").mkdir(exist_ok=True)
    staging_root = output_directory / ".staging"
    staging_root.mkdir(exist_ok=True)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    staging = staging_root / run_id
    published = output_directory / "runs" / run_id
    try:
        _write_bundle(staging, report, daily, by_category_state, by_state, price_by_store,
                      by_weekday, by_month)
        staging.rename(published)
        _publish_pointer(output_directory, {
            "run_id": run_id,
            "run_directory": f"runs/{run_id}",
            "report": f"runs/{run_id}/eda_report.json",
            "html_report": f"runs/{run_id}/index.html",
        })
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingestion-run", type=Path,
                        help="validated ingestion run directory; defaults to data/processed/m5/latest.json")
    parser.add_argument("--output-directory", type=Path,
                        default=PROJECT_ROOT / "data" / "processed" / "m5" / "eda")
    args = parser.parse_args()
    processed_root = PROJECT_ROOT / "data" / "processed" / "m5"
    if args.ingestion_run:
        run_directory = args.ingestion_run.resolve()
        ingestion_run_id = run_directory.name
    else:
        run_directory, ingestion_run_id = _read_latest_ingestion_run(processed_root)
    output_directory = args.output_directory
    if not output_directory.is_absolute():
        output_directory = PROJECT_ROOT / output_directory
    result = run_m5_eda(run_directory, output_directory, ingestion_run_id=ingestion_run_id)
    pointer = json.loads((output_directory / "latest.json").read_text(encoding="utf-8"))
    print(f"M5 EDA completed. Report: {result / 'eda_report.json'}")
    print(f"HTML report: {result / 'index.html'}")
    print(f"Latest pointer: {output_directory / 'latest.json'} ({pointer['run_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
