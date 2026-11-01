"""Compare replenishment policies using a shared synthetic risk snapshot."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any


POLICY_NAMES = {
    "no_order": "Place no replenishment order",
    "unbudgeted_risk_reorder": "Use risk-engine reorder suggestions without a budget cap",
    "budget_optimized": "Use the saved budget-constrained optimizer allocation",
}


def _validated_order_quantity(value: Any, *, series_id: str, source: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{series_id} {source} quantity must be a non-negative integer")
    return value


def compare_replenishment_policies(
    inventory_records: Sequence[Mapping[str, Any]],
    risk_results: Sequence[Mapping[str, Any]],
    optimization_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Score no-order, risk-rule, and optimized allocations against shared exposure.

    This is a scenario comparison against calculated risk exposure. It does not
    replay observed future demand or estimate realized service levels or savings.
    """
    if not inventory_records or not risk_results:
        raise ValueError("inventory records and risk results must both be non-empty")

    inventory_by_id: dict[str, Mapping[str, Any]] = {}
    for record in inventory_records:
        series_id = str(record.get("series_id", ""))
        if not series_id or series_id in inventory_by_id:
            raise ValueError("inventory series IDs must be present and unique")
        inventory_by_id[series_id] = record

    risk_by_id: dict[str, Mapping[str, Any]] = {}
    for result in risk_results:
        series_id = str(result.get("series_id", ""))
        if not series_id or series_id in risk_by_id:
            raise ValueError("risk series IDs must be present and unique")
        risk_by_id[series_id] = result
    if inventory_by_id.keys() != risk_by_id.keys():
        raise ValueError("inventory and risk series IDs do not match")

    optimizer = optimization_report.get("optimization")
    if not isinstance(optimizer, Mapping):
        raise ValueError("optimization report must contain an optimization object")
    if optimizer.get("status") not in {"optimal", "feasible_but_not_proven_optimal"}:
        raise ValueError("optimization report does not contain a usable solver allocation")
    budget = optimizer.get("budget")
    if isinstance(budget, bool) or not isinstance(budget, Real) or not math.isfinite(float(budget)):
        raise ValueError("optimization budget must be a finite number")

    optimized_orders: dict[str, int] = {}
    raw_orders = optimizer.get("orders")
    if not isinstance(raw_orders, list):
        raise ValueError("optimization orders must be a list")
    for order in raw_orders:
        if not isinstance(order, Mapping):
            raise ValueError("each optimization order must be an object")
        series_id = str(order.get("series_id", ""))
        if not series_id or series_id not in inventory_by_id or series_id in optimized_orders:
            raise ValueError("optimization orders must have known, unique series IDs")
        optimized_orders[series_id] = _validated_order_quantity(
            order.get("order_quantity_units"), series_id=series_id, source="optimized order"
        )

    policy_quantities: dict[str, dict[str, int]] = {
        "no_order": {series_id: 0 for series_id in inventory_by_id},
        "unbudgeted_risk_reorder": {},
        "budget_optimized": {
            series_id: optimized_orders.get(series_id, 0) for series_id in inventory_by_id
        },
    }
    for series_id, risk in risk_by_id.items():
        policy_quantities["unbudgeted_risk_reorder"][series_id] = _validated_order_quantity(
            risk.get("suggested_order_quantity_units"),
            series_id=series_id,
            source="risk suggestion",
        )

    detailed_rows: list[dict[str, Any]] = []
    policy_metrics = {
        policy_id: {
            "policy_id": policy_id,
            "policy_name": POLICY_NAMES[policy_id],
            "series_count": len(inventory_by_id),
            "total_order_units": 0,
            "total_exposure_units": 0,
            "covered_exposure_units": 0,
            "uncovered_exposure_units": 0,
            "risk_weighted_coverage_score": 0,
            "estimated_order_cost": 0.0,
            "estimated_order_cost_cents": 0,
        }
        for policy_id in POLICY_NAMES
    }

    for series_id in sorted(inventory_by_id):
        inventory = inventory_by_id[series_id]
        risk = risk_by_id[series_id]
        exposure = risk.get("stockout_exposure_units_at_service_level")
        if type(exposure) is not int or exposure < 0:
            raise ValueError(f"{series_id} stockout exposure must be a non-negative integer")
        probability = risk.get("stockout_probability_normal_approximation")
        if isinstance(probability, bool) or not isinstance(probability, Real):
            raise ValueError(f"{series_id} stockout probability must be numeric")
        probability = float(probability)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"{series_id} stockout probability must be between 0 and 1")
        pack_size = inventory.get("pack_size")
        minimum_order_quantity = inventory.get("minimum_order_quantity")
        if type(pack_size) is not int or pack_size < 1:
            raise ValueError(f"{series_id} pack_size must be a positive integer")
        if type(minimum_order_quantity) is not int or minimum_order_quantity < 1:
            raise ValueError(f"{series_id} minimum_order_quantity must be a positive integer")
        unit_cost = inventory.get("estimated_unit_cost")
        if isinstance(unit_cost, bool) or not isinstance(unit_cost, Real):
            raise ValueError(f"{series_id} estimated_unit_cost must be numeric")
        unit_cost = float(unit_cost)
        if not math.isfinite(unit_cost) or unit_cost < 0:
            raise ValueError(f"{series_id} estimated_unit_cost must be finite and non-negative")
        unit_cost_cents = math.ceil(unit_cost * 100 - 1e-9)
        priority_weight = max(1, round(probability * 10_000))

        row: dict[str, Any] = {
            "series_id": series_id,
            "stockout_exposure_units": exposure,
            "stockout_probability": probability,
            "priority_weight": priority_weight,
            "policies": {},
        }
        for policy_id, quantities in policy_quantities.items():
            quantity = quantities[series_id]
            if quantity and (quantity < minimum_order_quantity or quantity % pack_size):
                raise ValueError(
                    f"{series_id} {policy_id} order violates MOQ or pack-size constraints"
                )
            covered = min(quantity, exposure)
            cost_cents = quantity * unit_cost_cents
            policy = policy_metrics[policy_id]
            policy["total_order_units"] += quantity
            policy["total_exposure_units"] += exposure
            policy["covered_exposure_units"] += covered
            policy["uncovered_exposure_units"] += exposure - covered
            policy["risk_weighted_coverage_score"] += covered * priority_weight
            policy["estimated_order_cost_cents"] += cost_cents
            row["policies"][policy_id] = {
                "order_quantity_units": quantity,
                "covered_exposure_units": covered,
                "uncovered_exposure_units": exposure - covered,
                "estimated_order_cost": round(cost_cents / 100, 2),
            }
        detailed_rows.append(row)

    for policy in policy_metrics.values():
        policy["estimated_order_cost"] = round(policy["estimated_order_cost_cents"] / 100, 2)
        policy["exposure_coverage_fraction"] = (
            round(policy["covered_exposure_units"] / policy["total_exposure_units"], 6)
            if policy["total_exposure_units"]
            else None
        )
        policy["weighted_coverage_per_cost_unit"] = (
            round(policy["risk_weighted_coverage_score"] / policy["estimated_order_cost"], 4)
            if policy["estimated_order_cost"]
            else None
        )

    return {
        "comparison_basis": "same inventory snapshot, stockout exposure, probabilities, and synthetic unit costs",
        "optimizer_budget": round(float(budget), 2),
        "policies": list(policy_metrics.values()),
        "series_results": detailed_rows,
        "limitations": [
            "Coverage is calculated against the risk engine's exposure estimate; no future demand is replayed.",
            "The unbudgeted risk-reorder policy ignores a purchasing budget and is a reference scenario only.",
            "Inventory, supplier rules, probabilities, and costs include synthetic assumptions.",
            "Metrics are scenario comparisons, not measured service outcomes, profit, or savings.",
        ],
    }
