"""Budget-constrained purchase allocation using optional OR-Tools CP-SAT."""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any


class OptimizerDependencyError(RuntimeError):
    """Raised when OR-Tools is not installed for the optional optimizer."""


def _load_cp_model() -> Any:
    try:
        return importlib.import_module("ortools.sat.python.cp_model")
    except ImportError as error:
        raise OptimizerDependencyError(
            "OR-Tools is optional and is not installed. Install requirements-optimization.txt "
            "to run the purchasing optimizer."
        ) from error


def build_purchase_candidates(
    inventory_records: Sequence[Mapping[str, Any]],
    risk_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and join synthetic inventory with risk-derived order gaps."""
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
        missing_risk = sorted(inventory_by_id.keys() - risk_by_id.keys())
        missing_inventory = sorted(risk_by_id.keys() - inventory_by_id.keys())
        raise ValueError(
            f"inventory and risk series do not match; missing_risk={missing_risk}, "
            f"missing_inventory={missing_inventory}"
        )

    candidates: list[dict[str, Any]] = []
    for series_id in sorted(inventory_by_id):
        inventory = inventory_by_id[series_id]
        risk = risk_by_id[series_id]
        for field in ("minimum_order_quantity", "pack_size"):
            if type(inventory.get(field)) is not int or inventory[field] < 1:
                raise ValueError(f"{series_id} {field} must be a positive integer")
        minimum_order_quantity = inventory["minimum_order_quantity"]
        pack_size = inventory["pack_size"]
        if minimum_order_quantity % pack_size:
            raise ValueError(f"{series_id} MOQ must be a multiple of pack size")

        exposure = risk.get("stockout_exposure_units_at_service_level")
        if type(exposure) is not int or exposure < 0:
            raise ValueError(f"{series_id} stockout exposure must be a non-negative integer")
        probability = risk.get("stockout_probability_normal_approximation")
        if isinstance(probability, bool) or not isinstance(probability, Real):
            raise ValueError(f"{series_id} stockout probability must be numeric")
        probability = float(probability)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"{series_id} stockout probability must be between 0 and 1")

        unit_cost = inventory.get("estimated_unit_cost")
        if isinstance(unit_cost, bool) or not isinstance(unit_cost, Real):
            raise ValueError(f"{series_id} estimated unit cost must be numeric")
        unit_cost = float(unit_cost)
        if not math.isfinite(unit_cost) or unit_cost < 0:
            raise ValueError(f"{series_id} estimated unit cost must be finite and non-negative")

        minimum_order_packs = minimum_order_quantity // pack_size
        required_packs = math.ceil(exposure / pack_size) if exposure else 0
        max_order_packs = max(minimum_order_packs, required_packs) if exposure else 0
        candidates.append({
            "series_id": series_id,
            "stockout_exposure_units": exposure,
            "stockout_probability": probability,
            "priority_weight": max(1, round(probability * 10_000)),
            "pack_size": pack_size,
            "minimum_order_quantity": minimum_order_quantity,
            "minimum_order_packs": minimum_order_packs,
            "maximum_order_packs": max_order_packs,
            "unit_cost_proxy": unit_cost,
            "unit_cost_cents_conservative": math.ceil(unit_cost * 100 - 1e-9),
        })
    return candidates


def _map_solver_status(status: int, cp_model: Any) -> str:
    if status == cp_model.OPTIMAL:
        return "optimal"
    if status == cp_model.FEASIBLE:
        return "feasible_but_not_proven_optimal"
    if status == cp_model.INFEASIBLE:
        return "infeasible"
    if status == cp_model.UNKNOWN:
        return "timed_out"
    return "failed"


def solve_purchase_allocation(
    inventory_records: Sequence[Mapping[str, Any]],
    risk_results: Sequence[Mapping[str, Any]],
    *,
    budget: float,
    max_seconds: float = 10.0,
    cp_model_module: Any | None = None,
) -> dict[str, Any]:
    """Maximize risk-weighted shortage coverage under budget, MOQ, and pack rules.

    Orders are optional (zero is always allowed). A positive order must meet
    the supplier MOQ, use whole packs, and cannot exceed the pack-rounded risk
    exposure except where MOQ requires the larger minimum order. Unit costs are
    rounded up to cents for the budget constraint to avoid spending over budget.
    """
    if isinstance(budget, bool) or not isinstance(budget, Real):
        raise ValueError("budget must be a non-negative number")
    if not math.isfinite(float(budget)) or budget < 0:
        raise ValueError("budget must be a finite non-negative number")
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, Real):
        raise ValueError("max_seconds must be a positive number")
    if not math.isfinite(float(max_seconds)) or max_seconds <= 0:
        raise ValueError("max_seconds must be a finite positive number")
    budget_cents = math.floor(float(budget) * 100 + 1e-9)
    candidates = build_purchase_candidates(inventory_records, risk_results)
    if cp_model_module is None:
        cp_model_module = _load_cp_model()

    model = cp_model_module.CpModel()
    purchase_variables: dict[str, Any] = {}
    covered_variables: dict[str, Any] = {}
    total_cost_terms: list[Any] = []
    objective_terms: list[Any] = []

    for index, candidate in enumerate(candidates):
        series_id = candidate["series_id"]
        packs = model.new_int_var(
            0,
            candidate["maximum_order_packs"],
            f"packs_{index}",
        )
        order_active = model.new_bool_var(f"order_active_{index}")
        model.add(packs >= candidate["minimum_order_packs"]).only_enforce_if(order_active)
        model.add(packs <= candidate["maximum_order_packs"] * order_active)
        order_units = model.new_int_var(
            0,
            candidate["maximum_order_packs"] * candidate["pack_size"],
            f"units_{index}",
        )
        model.add(order_units == packs * candidate["pack_size"])
        covered_units = model.new_int_var(
            0,
            candidate["stockout_exposure_units"],
            f"covered_{index}",
        )
        model.add(covered_units <= order_units)

        purchase_variables[series_id] = (packs, order_units)
        covered_variables[series_id] = covered_units
        total_cost_terms.append(
            order_units * candidate["unit_cost_cents_conservative"]
        )
        objective_terms.append(covered_units * candidate["priority_weight"])

    model.add(sum(total_cost_terms) <= budget_cents)
    model.maximize(sum(objective_terms))

    solver = cp_model_module.CpSolver()
    solver.parameters.max_time_in_seconds = float(max_seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver_status = solver.solve(model)
    result_status = _map_solver_status(solver_status, cp_model_module)

    assignments: list[dict[str, Any]] = []
    total_spend_cents = 0
    total_covered_units = 0
    if result_status in {"optimal", "feasible_but_not_proven_optimal"}:
        candidate_by_id = {candidate["series_id"]: candidate for candidate in candidates}
        for series_id, (packs_var, units_var) in purchase_variables.items():
            packs = int(solver.value(packs_var))
            units = int(solver.value(units_var))
            covered = int(solver.value(covered_variables[series_id]))
            candidate = candidate_by_id[series_id]
            cost_cents = units * candidate["unit_cost_cents_conservative"]
            total_spend_cents += cost_cents
            total_covered_units += covered
            if units:
                assignments.append({
                    "series_id": series_id,
                    "order_packs": packs,
                    "order_quantity_units": units,
                    "risk_exposure_units": candidate["stockout_exposure_units"],
                    "covered_risk_units": covered,
                    "uncovered_risk_units": max(0, candidate["stockout_exposure_units"] - covered),
                    "stockout_probability": candidate["stockout_probability"],
                    "priority_weight": candidate["priority_weight"],
                    "unit_cost_proxy": candidate["unit_cost_proxy"],
                    "conservative_order_cost": round(cost_cents / 100, 2),
                    "cost_basis": "synthetic unit-cost proxy rounded up to cents",
                })

    output = {
        "status": result_status,
        "budget": round(budget_cents / 100, 2),
        "budget_cents": budget_cents,
        "total_spend": round(total_spend_cents / 100, 2),
        "total_spend_cents": total_spend_cents,
        "remaining_budget": round((budget_cents - total_spend_cents) / 100, 2),
        "units_covered": total_covered_units,
        "orders": assignments,
        "candidate_count": len(candidates),
        "solver": "OR-Tools CP-SAT",
        "solver_wall_time_seconds": round(float(solver.wall_time), 4),
        "objective_value": float(solver.objective_value) if result_status in {
            "optimal", "feasible_but_not_proven_optimal"
        } else None,
        "risk_weighted_coverage_score": float(solver.objective_value) if result_status in {
            "optimal", "feasible_but_not_proven_optimal"
        } else None,
        "best_objective_bound": float(solver.best_objective_bound) if result_status in {
            "optimal", "feasible_but_not_proven_optimal"
        } else None,
        "status_notes": {
            "optimal": "best feasible risk-weighted coverage proven by CP-SAT",
            "feasible_but_not_proven_optimal": "feasible allocation found; optimality not proven within solver limits",
            "infeasible": "the supplied model constraints have no feasible allocation",
            "timed_out": "CP-SAT stopped without finding or proving an allocation",
            "failed": "CP-SAT returned an invalid or unsupported status",
        }[result_status],
        "objective_definition": "maximize covered stockout-exposure units weighted by reported stockout probability",
        "limitations": [
            "Inventory, supplier constraints, and unit costs are synthetic assumptions.",
            "The optimizer only addresses reported lead-time stockout exposure; it does not optimize expiry markdowns or retailer profit.",
            "Orders are recommendations for a simulated scenario and are never submitted to a supplier.",
        ],
    }
    return output
