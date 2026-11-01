"""Expiry-aware markdown scenarios under explicit demand-response assumptions."""

from __future__ import annotations

import math
from datetime import date
from collections.abc import Sequence
from numbers import Real
from typing import Any


DEFAULT_DISCOUNT_LEVELS = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50)
DEMAND_LIFT_PER_DISCOUNT = 0.5
MAX_DEMAND_LIFT = 0.50


def _simulate_fifo_scenario(
    batches: list[dict[str, Any]],
    expiry_offsets: dict[str, int],
    baseline_forecast: Sequence[float],
    demand_multiplier: float,
) -> tuple[float, float, float]:
    """Return sold units, expired units, and unfilled forecast demand."""
    ordered_batches = sorted(
        batches,
        key=lambda batch: (expiry_offsets[str(batch["batch_id"])], str(batch["batch_id"])),
    )
    remaining = {str(batch["batch_id"]): float(batch["quantity_units"]) for batch in ordered_batches}
    sold_by_batch = {batch_id: 0.0 for batch_id in remaining}
    expired_units = 0.0
    unfilled_demand = 0.0

    for day_index, baseline_demand in enumerate(baseline_forecast):
        for batch in ordered_batches:
            batch_id = str(batch["batch_id"])
            if expiry_offsets[batch_id] <= day_index and remaining[batch_id] > 0:
                expired_units += remaining[batch_id]
                remaining[batch_id] = 0.0

        demand_left = baseline_demand * demand_multiplier
        for batch in ordered_batches:
            batch_id = str(batch["batch_id"])
            if expiry_offsets[batch_id] <= day_index:
                continue
            sold = min(remaining[batch_id], demand_left)
            remaining[batch_id] -= sold
            sold_by_batch[batch_id] += sold
            demand_left -= sold
            if demand_left <= 0:
                break
        unfilled_demand += max(0.0, demand_left)

    for batch in ordered_batches:
        batch_id = str(batch["batch_id"])
        if expiry_offsets[batch_id] <= len(baseline_forecast):
            expired_units += remaining[batch_id]

    return sum(sold_by_batch.values()), expired_units, unfilled_demand


def compare_markdown_scenarios(
    inventory: dict[str, Any],
    forecast_daily_units: Sequence[float],
    *,
    discount_levels: Sequence[float] = DEFAULT_DISCOUNT_LEVELS,
) -> dict[str, Any]:
    """Compare hypothetical markdown levels for a product/store's expiry lots.

    A discount fraction `d` scales baseline demand by `1 + 0.5*d`, capped at
    a 50% lift. The response is a documented simulation assumption, not an
    elasticity estimated from public M5 data. No price is changed by this code.
    """
    required = {
        "series_id", "as_of_date", "on_hand_units", "inventory_batches",
        "observed_retail_price", "estimated_unit_cost",
    }
    missing = sorted(required - inventory.keys())
    if missing:
        raise ValueError(f"inventory record is missing required fields: {missing}")
    try:
        as_of_date = date.fromisoformat(str(inventory["as_of_date"]))
    except ValueError as error:
        raise ValueError("inventory as_of_date must be an ISO date") from error

    raw_price = inventory["observed_retail_price"]
    raw_unit_cost = inventory["estimated_unit_cost"]
    if (
        isinstance(raw_price, bool)
        or not isinstance(raw_price, Real)
        or isinstance(raw_unit_cost, bool)
        or not isinstance(raw_unit_cost, Real)
    ):
        raise ValueError("retail price and unit cost must be numeric")
    retail_price = float(raw_price)
    unit_cost = float(raw_unit_cost)
    if not math.isfinite(retail_price) or retail_price <= 0:
        raise ValueError("observed_retail_price must be finite and positive")
    if not math.isfinite(unit_cost) or unit_cost < 0:
        raise ValueError("estimated_unit_cost must be finite and non-negative")
    if type(inventory["on_hand_units"]) is not int or inventory["on_hand_units"] < 0:
        raise ValueError("on_hand_units must be a non-negative integer")
    if not isinstance(forecast_daily_units, Sequence) or isinstance(forecast_daily_units, (str, bytes)):
        raise ValueError("forecast_daily_units must be a sequence of numbers")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in forecast_daily_units):
        raise ValueError("forecast_daily_units must contain numeric values")
    forecast = [float(value) for value in forecast_daily_units]
    if not forecast or any(not math.isfinite(value) or value < 0 for value in forecast):
        raise ValueError("forecast_daily_units must contain finite non-negative values")
    if not discount_levels:
        raise ValueError("discount_levels must contain at least one value")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in discount_levels):
        raise ValueError("discount levels must be numeric fractions")
    discounts = [float(value) for value in discount_levels]
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in discounts):
        raise ValueError("discount levels must be finite fractions between 0 and 1")
    if len(set(discounts)) != len(discounts):
        raise ValueError("discount levels must be unique")

    batches = list(inventory["inventory_batches"])
    batch_ids: set[str] = set()
    for batch in batches:
        batch_id = str(batch.get("batch_id", ""))
        if not batch_id or batch_id in batch_ids:
            raise ValueError("inventory batch IDs must be present and unique")
        batch_ids.add(batch_id)
        if type(batch.get("quantity_units")) is not int or batch["quantity_units"] < 0:
            raise ValueError("inventory batch quantity_units must be a non-negative integer")
    if sum(batch["quantity_units"] for batch in batches) != inventory["on_hand_units"]:
        raise ValueError("inventory batch quantities must sum to on_hand_units")
    expiry_offsets: dict[str, int] = {}
    for batch in batches:
        batch_id = str(batch.get("batch_id", ""))
        if not batch_id or batch_id in expiry_offsets:
            raise ValueError("inventory batch IDs must be present and unique")
        if type(batch.get("quantity_units")) is not int or batch["quantity_units"] < 0:
            raise ValueError("inventory batch quantity_units must be a non-negative integer")
        try:
            expiry_date = date.fromisoformat(str(batch["expiry_date"]))
        except (KeyError, ValueError) as error:
            raise ValueError("each inventory batch must have an ISO expiry_date") from error
        expiry_offsets[batch_id] = (expiry_date - as_of_date).days
    if any(offset > len(forecast) for offset in expiry_offsets.values()):
        raise ValueError("forecast horizon must cover every inventory batch expiry")

    scenario_results: list[dict[str, Any]] = []
    for discount in sorted(discounts):
        demand_multiplier = min(1 + DEMAND_LIFT_PER_DISCOUNT * discount, 1 + MAX_DEMAND_LIFT)
        sold_units, expired_units, unfilled_demand = _simulate_fifo_scenario(
            batches,
            expiry_offsets,
            forecast,
            demand_multiplier,
        )
        discounted_price = retail_price * (1 - discount)
        revenue = sold_units * discounted_price
        gross_margin_on_sold = sold_units * (discounted_price - unit_cost)
        expired_cost_proxy = expired_units * unit_cost
        scenario_results.append({
            "discount_fraction": discount,
            "discount_percent": round(discount * 100, 2),
            "hypothetical_price": round(discounted_price, 4),
            "demand_multiplier": round(demand_multiplier, 4),
            "sold_units": round(sold_units, 4),
            "units_expiring_unsold": round(expired_units, 4),
            "unfilled_forecast_units": round(unfilled_demand, 4),
            "estimated_revenue": round(revenue, 4),
            "estimated_gross_margin_on_sold": round(gross_margin_on_sold, 4),
            "expired_cost_proxy": round(expired_cost_proxy, 4),
            "estimated_net_value_after_expiry": round(gross_margin_on_sold - expired_cost_proxy, 4),
        })

    baseline = next((result for result in scenario_results if result["discount_fraction"] == 0), None)
    highest_value = max(
        scenario_results,
        key=lambda result: (
            result["estimated_net_value_after_expiry"],
            -result["discount_fraction"],
        ),
    )
    if baseline is not None:
        for result in scenario_results:
            result["net_value_change_vs_no_markdown"] = round(
                result["estimated_net_value_after_expiry"]
                - baseline["estimated_net_value_after_expiry"],
                4,
            )

    return {
        "series_id": str(inventory["series_id"]),
        "as_of_date": as_of_date.isoformat(),
        "observed_retail_price": retail_price,
        "synthetic_unit_cost_proxy": unit_cost,
        "demand_response_assumption": {
            "formula": "demand_multiplier = min(1 + 0.5 * discount_fraction, 1.5)",
            "source": "declared simulation assumption; not estimated from M5 and not causal evidence",
        },
        "expiry_allocation": "earliest-expiry-first",
        "scenarios": scenario_results,
        "highest_modeled_net_value_scenario": highest_value,
        "recommended_live_price_change": False,
        "provenance": {
            "inventory": "synthetic",
            "retail_price": "observed M5 price reference",
            "unit_cost": "synthetic proxy",
            "forecast": "recent-average point forecast from observed M5 sales",
        },
    }
