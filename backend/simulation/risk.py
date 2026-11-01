"""Deterministic inventory-risk calculations from forecasts and inventory evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from statistics import NormalDist, stdev
from numbers import Real
from typing import Any


def calculate_inventory_risk(
    inventory: dict[str, Any],
    forecast_daily_units: Sequence[float],
    *,
    service_level: float = 0.95,
) -> dict[str, Any]:
    """Calculate stockout, replenishment, and expiry indicators for one series.

    Lead-time demand uncertainty uses the sample standard deviation of recent
    observed daily sales and an independent, normal-demand approximation. The
    point forecast is supplied by the caller; the result is an assumption-based
    decision aid, not a calibrated probability for latent retail demand.
    """
    required_fields = {
        "series_id", "as_of_date", "on_hand_units", "inventory_batches",
        "incoming_purchase_order", "lead_time_days", "minimum_order_quantity",
        "pack_size", "estimated_unit_cost", "recent_observed_sales",
    }
    missing = sorted(required_fields - inventory.keys())
    if missing:
        raise ValueError(f"inventory record is missing required fields: {missing}")
    if not 0 < service_level < 1:
        raise ValueError("service_level must be between 0 and 1")

    try:
        as_of_date = date.fromisoformat(str(inventory["as_of_date"]))
    except ValueError as error:
        raise ValueError("inventory as_of_date must be an ISO date") from error

    integer_fields = (
        "lead_time_days", "on_hand_units", "minimum_order_quantity", "pack_size"
    )
    for field in integer_fields:
        if type(inventory[field]) is not int:
            raise ValueError(f"{field} must be an integer")
    lead_time_days = inventory["lead_time_days"]
    on_hand_units = inventory["on_hand_units"]
    minimum_order_quantity = inventory["minimum_order_quantity"]
    pack_size = inventory["pack_size"]
    unit_cost = float(inventory["estimated_unit_cost"])
    if lead_time_days < 1 or on_hand_units < 0 or minimum_order_quantity < 1 or pack_size < 1:
        raise ValueError("lead time, stock, MOQ, and pack size are outside valid ranges")
    if minimum_order_quantity % pack_size:
        raise ValueError("minimum_order_quantity must be a multiple of pack_size")
    if not math.isfinite(unit_cost) or unit_cost < 0:
        raise ValueError("estimated_unit_cost must be finite and non-negative")

    raw_recent_sales = inventory["recent_observed_sales"]
    if not isinstance(raw_recent_sales, Sequence) or isinstance(raw_recent_sales, (str, bytes)):
        raise ValueError("recent_observed_sales must be a sequence of numbers")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in raw_recent_sales):
        raise ValueError("recent_observed_sales must contain numeric values")
    recent_sales = [float(value) for value in raw_recent_sales]
    if not recent_sales or any(not math.isfinite(value) or value < 0 for value in recent_sales):
        raise ValueError("recent_observed_sales must contain finite non-negative values")
    if not isinstance(forecast_daily_units, Sequence) or isinstance(forecast_daily_units, (str, bytes)):
        raise ValueError("forecast_daily_units must be a sequence of numbers")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in forecast_daily_units):
        raise ValueError("forecast_daily_units must contain numeric values")
    forecast = [float(value) for value in forecast_daily_units]
    if not forecast or any(not math.isfinite(value) or value < 0 for value in forecast):
        raise ValueError("forecast_daily_units must contain finite non-negative values")
    if len(forecast) < lead_time_days:
        raise ValueError("forecast horizon must cover the supplier lead time")

    batches = list(inventory["inventory_batches"])
    if sum(int(batch["quantity_units"]) for batch in batches) != on_hand_units:
        raise ValueError("inventory batch quantities must sum to on_hand_units")
    expiry_offsets: dict[str, int] = {}
    for batch in batches:
        if type(batch.get("quantity_units")) is not int or batch["quantity_units"] < 0:
            raise ValueError("inventory batch quantity_units must be a non-negative integer")
        if not batch.get("batch_id") or str(batch["batch_id"]) in expiry_offsets:
            raise ValueError("inventory batch IDs must be present and unique")
        try:
            expiry = date.fromisoformat(str(batch["expiry_date"]))
        except (KeyError, ValueError) as error:
            raise ValueError("each inventory batch must have an ISO expiry_date") from error
        expiry_offsets[str(batch["batch_id"])] = (expiry - as_of_date).days
    if any(offset > len(forecast) for offset in expiry_offsets.values()):
        raise ValueError("forecast horizon must cover every inventory batch expiry")

    expected_lead_time_demand = sum(forecast[:lead_time_days])
    daily_demand_std = stdev(recent_sales) if len(recent_sales) > 1 else 0.0
    lead_time_demand_std = daily_demand_std * math.sqrt(lead_time_days)
    z_score = NormalDist().inv_cdf(service_level)
    lead_time_demand_quantile = max(
        0.0,
        expected_lead_time_demand + z_score * lead_time_demand_std,
    )
    reorder_point_units = math.ceil(lead_time_demand_quantile)
    safety_stock_units = max(0.0, lead_time_demand_quantile - expected_lead_time_demand)

    incoming_order = inventory["incoming_purchase_order"]
    incoming_units_within_lead_time = 0
    if incoming_order is not None:
        try:
            arrival_date = date.fromisoformat(str(incoming_order["expected_arrival_date"]))
            order_quantity = incoming_order["quantity_units"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("incoming purchase order has invalid date or quantity") from error
        if type(order_quantity) is not int:
            raise ValueError("incoming purchase quantity must be an integer")
        if order_quantity < 0 or order_quantity % pack_size:
            raise ValueError("incoming purchase quantity must be non-negative and match pack_size")
        days_until_arrival = (arrival_date - as_of_date).days
        if days_until_arrival < 0:
            raise ValueError("incoming purchase arrival date must not precede as_of_date")
        if days_until_arrival <= lead_time_days:
            incoming_units_within_lead_time = order_quantity

    available_supply_at_lead_time = on_hand_units + incoming_units_within_lead_time
    if lead_time_demand_std == 0:
        stockout_probability = float(expected_lead_time_demand > available_supply_at_lead_time)
    else:
        stockout_probability = 1 - NormalDist(
            mu=expected_lead_time_demand, sigma=lead_time_demand_std
        ).cdf(available_supply_at_lead_time)
        stockout_probability = min(1.0, max(0.0, stockout_probability))

    order_gap = max(0, reorder_point_units - available_supply_at_lead_time)
    suggested_order_quantity = 0
    if order_gap:
        suggested_order_quantity = max(
            minimum_order_quantity,
            math.ceil(order_gap / pack_size) * pack_size,
        )

    average_forecast = sum(forecast[: min(28, len(forecast))]) / min(28, len(forecast))
    overstock_review_period_days = 28
    average_forecast_for_policy = round(average_forecast, 4)
    overstock_target_units = reorder_point_units + math.ceil(
        average_forecast_for_policy * overstock_review_period_days
    )
    overstock_units = max(0, available_supply_at_lead_time - overstock_target_units)
    stock_coverage_days = on_hand_units / average_forecast if average_forecast > 0 else None

    ordered_batches = sorted(
        batches,
        key=lambda row: (expiry_offsets[str(row["batch_id"])], row["batch_id"]),
    )
    remaining_by_batch = {
        str(batch["batch_id"]): float(batch["quantity_units"])
        for batch in ordered_batches
    }
    expiry_units_at_risk = 0.0
    for day_index, daily_demand in enumerate(forecast):
        for batch in ordered_batches:
            batch_id = str(batch["batch_id"])
            if expiry_offsets[batch_id] <= day_index and remaining_by_batch[batch_id] > 0:
                expiry_units_at_risk += remaining_by_batch[batch_id]
                remaining_by_batch[batch_id] = 0.0

        demand_left = daily_demand
        for batch in ordered_batches:
            batch_id = str(batch["batch_id"])
            if expiry_offsets[batch_id] <= day_index:
                continue
            consumed = min(remaining_by_batch[batch_id], demand_left)
            remaining_by_batch[batch_id] -= consumed
            demand_left -= consumed
            if demand_left <= 0:
                break

    for batch in ordered_batches:
        batch_id = str(batch["batch_id"])
        if expiry_offsets[batch_id] <= len(forecast):
            expiry_units_at_risk += remaining_by_batch[batch_id]

    return {
        "series_id": str(inventory["series_id"]),
        "as_of_date": as_of_date.isoformat(),
        "service_level": service_level,
        "forecast_days_available": len(forecast),
        "forecast_average_daily_units": round(average_forecast, 4),
        "stock_coverage_days": round(stock_coverage_days, 4) if stock_coverage_days is not None else None,
        "lead_time_days": lead_time_days,
        "expected_lead_time_demand_units": round(expected_lead_time_demand, 4),
        "lead_time_demand_std_units": round(lead_time_demand_std, 4),
        "lead_time_demand_quantile_units": round(lead_time_demand_quantile, 4),
        "safety_stock_units": round(safety_stock_units, 4),
        "reorder_point_units": reorder_point_units,
        "incoming_units_within_lead_time": incoming_units_within_lead_time,
        "available_supply_at_lead_time": available_supply_at_lead_time,
        "overstock_target_units": overstock_target_units,
        "overstock_units": overstock_units,
        "stockout_probability_normal_approximation": round(stockout_probability, 6),
        "stockout_exposure_units_at_service_level": max(
            0, reorder_point_units - available_supply_at_lead_time
        ),
        "suggested_order_quantity_units": suggested_order_quantity,
        "expiry_units_at_risk_fifo": round(expiry_units_at_risk, 4),
        "expiry_value_at_risk_proxy": round(expiry_units_at_risk * unit_cost, 4),
        "assumptions": {
            "forecast_source": "recent-average point forecast from trailing observed M5 sales",
            "lead_time_uncertainty": "normal approximation; recent daily sales are independent with stable variance",
            "expiry_consumption": "forecast demand consumes batches in earliest-expiry-first order",
            "unit_cost": "synthetic proxy from inventory record; not actual procurement cost",
            "overstock": (
                "available supply above reorder point plus 28 days of average forecast demand "
                "(daily average rounded to four decimals); "
                "review period is a prototype assumption"
            ),
        },
        "provenance": {
            "inventory_data": inventory.get("provenance", {}).get("inventory_and_supplier_fields", "unknown"),
            "demand_data": "observed M5 sales reference; may be stockout-censored",
        },
    }
