"""Seeded simulator for operational inventory fields missing from public M5."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from numbers import Real
from typing import Any


SIMULATOR_VERSION = "1.0.0"
DEMAND_WINDOW_DAYS = 28
UNIT_COST_SHARE_OF_RETAIL = 0.65


def generate_simulated_inventory(
    series_records: Sequence[Mapping[str, Any]],
    *,
    as_of_date: date,
    seed: int = 20261029,
) -> list[dict[str, Any]]:
    """Generate reproducible, explicitly synthetic operational records.

    `recent_sales` and `observed_retail_price` are observed M5 references. All
    inventory, expiry, supplier, purchase-order, and unit-cost fields are
    simulated. Inputs are sorted by series ID before using the seeded RNG so
    that input iteration order cannot change the generated values.
    """
    if type(as_of_date) is not date:
        raise ValueError("as_of_date must be a datetime.date")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if not series_records:
        raise ValueError("at least one series record is required")

    required_fields = {
        "series_id", "item_id", "store_id", "state_id", "category_id",
        "recent_sales", "observed_retail_price",
    }
    records_by_id: dict[str, Mapping[str, Any]] = {}
    for record in series_records:
        missing = sorted(required_fields - record.keys())
        if missing:
            raise ValueError(f"input record is missing required fields: {missing}")
        series_id = str(record["series_id"])
        if not series_id:
            raise ValueError("series_id must not be empty")
        for field in ("item_id", "store_id", "state_id", "category_id"):
            if record[field] is None or not str(record[field]).strip():
                raise ValueError(f"series {series_id} has an empty {field}")
        if series_id in records_by_id:
            raise ValueError(f"duplicate series_id: {series_id}")

        try:
            sales = list(record["recent_sales"])
        except TypeError as error:
            raise ValueError(f"series {series_id} recent_sales must be a sequence") from error
        if not sales:
            raise ValueError(f"series {series_id} must have at least one recent sales value")
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in sales
        ):
            raise ValueError(f"series {series_id} has invalid recent sales values")
        if isinstance(record["observed_retail_price"], bool) or not isinstance(record["observed_retail_price"], Real):
            raise ValueError(f"series {series_id} observed_retail_price must be numeric")
        retail_price = float(record["observed_retail_price"])
        if not math.isfinite(retail_price) or retail_price <= 0:
            raise ValueError(f"series {series_id} observed_retail_price must be finite and positive")
        records_by_id[series_id] = record

    generator = random.Random(seed)
    output: list[dict[str, Any]] = []
    for series_id in sorted(records_by_id):
        source = records_by_id[series_id]
        recent_sales = [float(value) for value in source["recent_sales"][-DEMAND_WINDOW_DAYS:]]
        average_daily_sales = sum(recent_sales) / len(recent_sales)
        stock_cover_days = generator.randint(0, 42)
        on_hand_units = int(round(average_daily_sales * stock_cover_days))

        batch_count = 0 if on_hand_units == 0 else generator.randint(1, min(3, on_hand_units))
        lot_quantities = [on_hand_units // batch_count] * batch_count if batch_count else []
        for index in generator.sample(range(batch_count), on_hand_units % batch_count) if batch_count else []:
            lot_quantities[index] += 1
        batches = [
            {
                "batch_id": f"{series_id}-LOT-{index + 1:02d}",
                "quantity_units": quantity,
                "expiry_date": (as_of_date + timedelta(days=generator.randint(14, 120))).isoformat(),
                "provenance": "synthetic",
            }
            for index, quantity in enumerate(lot_quantities)
        ]

        lead_time_days = generator.randint(2, 14)
        pack_size = generator.choice([1, 6, 12])
        minimum_order_packs = generator.randint(1, 4)
        minimum_order_quantity = pack_size * minimum_order_packs
        inbound_order = None
        if generator.random() < 0.25:
            order_packs = generator.randint(minimum_order_packs, minimum_order_packs + 4)
            inbound_order = {
                "purchase_order_id": f"{series_id}-PO-SIM",
                "quantity_units": order_packs * pack_size,
                "expected_arrival_date": (as_of_date + timedelta(days=lead_time_days)).isoformat(),
                "status": "open",
                "provenance": "synthetic",
            }

        output.append({
            "series_id": series_id,
            "item_id": str(source["item_id"]),
            "store_id": str(source["store_id"]),
            "state_id": str(source["state_id"]),
            "category_id": str(source["category_id"]),
            "as_of_date": as_of_date.isoformat(),
            "on_hand_units": on_hand_units,
            "inventory_batches": batches,
            "incoming_purchase_order": inbound_order,
            "lead_time_days": lead_time_days,
            "minimum_order_quantity": minimum_order_quantity,
            "pack_size": pack_size,
            "observed_retail_price": float(source["observed_retail_price"]),
            "estimated_unit_cost": round(float(source["observed_retail_price"]) * UNIT_COST_SHARE_OF_RETAIL, 4),
            "average_daily_observed_sales": average_daily_sales,
            "recent_observed_sales": recent_sales,
            "observations_in_demand_reference": len(recent_sales),
            "provenance": {
                "inventory_and_supplier_fields": "synthetic",
                "unit_cost": "synthetic proxy: 65% of observed retail price",
                "demand_reference": "observed M5 sales; not latent demand or a forecast",
                "retail_price": "observed M5 selling price",
                "simulator_version": SIMULATOR_VERSION,
                "simulator_seed": seed,
            },
        })
    return output
