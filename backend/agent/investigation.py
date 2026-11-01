"""LangGraph workflow for deterministic, evidence-backed investigations."""

from __future__ import annotations

import math
import operator
import uuid
from time import perf_counter
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from backend.agent.business_intelligence_agent import run_business_intelligence_agent
from backend.agent.contracts import AgentName, EvidenceDomain, contains_numeric_claim
from backend.agent.decision_agent import run_decision_agent
from backend.agent.decision_evidence import (
    DecisionEvidenceError,
    DecisionScenario,
    read_latest_decision_reports,
)
from backend.agent.forecast_agent import run_forecast_agent
from backend.agent.inventory_risk_agent import run_inventory_risk_agent
from backend.agent.ollama import check_model_available, generate_explanation
from backend.agent.supervisor import classify_request
from backend.api.config import PROJECT_ROOT, Settings
from backend.persistence.models import (
    ForecastMetric,
    ForecastRun,
    InventoryRiskItem,
    InventoryRiskRun,
)

EvidenceType = Literal["forecast", "inventory-risk"]


class InvestigationState(TypedDict, total=False):
    """Small serializable state passed between graph nodes."""

    evidence_type: EvidenceType
    run_id: str
    question: str | None
    evaluation_period: Literal["rolling_origin", "held_out"]
    evidence: list[dict[str, Any]]
    tool_history: list[dict[str, str]]
    status: Literal["completed", "not_found", "validation_failed", "needs_clarification", "pending"]
    answer: str
    schema_valid: bool
    numeric_claims_valid: bool
    evidence_references_valid: bool
    explanation_source: str
    provider: str
    model_name: str | None
    llm_used: bool
    llm_fallback_reason: str | None
    domains: list[EvidenceDomain]
    run_ids: dict[str, str]
    requested_domains: list[EvidenceDomain] | None
    requested_run_ids: dict[str, str]
    selected_domains: list[EvidenceDomain]
    source_run_ids: dict[str, list[str]]
    decision_scenarios: list[DecisionScenario]
    include_business_intelligence: bool
    selected_agents: list[AgentName]
    route: dict[str, Any] | None
    route_failure: str | None
    model_available: bool
    preflight_failure: str | None
    classifier_called: bool
    classifier_duration_ms: int
    classifier_ollama_total_duration_ns: int | None
    classifier_output_tokens: int | None
    agent_name: AgentName
    agent_results: Annotated[list[dict[str, Any]], operator.add]
    ollama_call_count: int
    llm_inference_duration_ms: int
    database_retrieval_duration_ms: int
    agent_output_valid: bool
    source_run_id: str


def _read_forecast(state: InvestigationState, engine: Engine) -> InvestigationState:
    """Read one forecast run and its metrics for the selected period."""
    with Session(engine) as session:
        run = session.get(ForecastRun, state["run_id"])
        if run is None:
            return {
                "status": "not_found",
                "evidence": [],
                "tool_history": [{"tool_name": "read_forecast_run", "status": "not_found"}],
            }
        metrics = session.scalars(
            select(ForecastMetric)
            .where(
                ForecastMetric.run_id == state["run_id"],
                ForecastMetric.evaluation_period == state["evaluation_period"],
            )
            .order_by(ForecastMetric.mae, ForecastMetric.model_name)
        ).all()
        facts = {
            "evaluation_period": state["evaluation_period"],
            "state_id": run.state_id,
            "category_id": run.category_id,
            "series_count": run.series_count,
            "source": run.source,
            "metrics": [
                {
                    "model_name": metric.model_name,
                    "mae": metric.mae,
                    "wape": metric.wape,
                    "bias": metric.bias,
                    "observations": metric.observations,
                }
                for metric in metrics
            ],
        }
    return {
        "status": "completed",
        "evidence": [{
            "evidence_id": f"forecast_run:{state['run_id']}:{state['evaluation_period']}",
            "source_table": "forecast_runs and forecast_metrics",
            "source_run_id": state["run_id"],
            "facts": facts,
        }],
        "tool_history": [{"tool_name": "read_forecast_run", "status": "completed"}],
    }


def _read_inventory_risk(state: InvestigationState, engine: Engine) -> InvestigationState:
    """Read a risk run and compute its highest recorded stockout and expiry values."""
    with Session(engine) as session:
        run = session.get(InventoryRiskRun, state["run_id"])
        if run is None:
            return {
                "status": "not_found",
                "evidence": [],
                "tool_history": [{"tool_name": "read_inventory_risk_run", "status": "not_found"}],
            }
        items = session.scalars(
            select(InventoryRiskItem)
            .where(InventoryRiskItem.run_id == state["run_id"])
            .order_by(InventoryRiskItem.series_id)
        ).all()
        highest_stockout = max(
            items,
            key=lambda item: (item.stockout_probability, item.series_id),
            default=None,
        )
        highest_expiry = max(
            items,
            key=lambda item: (item.expiry_units_at_risk, item.series_id),
            default=None,
        )
        facts = {
            "state_id": run.state_id,
            "category_id": run.category_id,
            "series_count": run.series_count,
            "service_level": run.service_level,
            "inventory_provenance": run.inventory_provenance,
            "demand_reference": run.demand_reference,
            "highest_stockout": None if highest_stockout is None else {
                "series_id": highest_stockout.series_id,
                "stockout_probability": highest_stockout.stockout_probability,
            },
            "highest_expiry": None if highest_expiry is None else {
                "series_id": highest_expiry.series_id,
                "expiry_units_at_risk": highest_expiry.expiry_units_at_risk,
            },
        }
    return {
        "status": "completed",
        "evidence": [{
            "evidence_id": f"inventory_risk_run:{state['run_id']}",
            "source_table": "inventory_risk_runs and inventory_risk_items",
            "source_run_id": state["run_id"],
            "facts": facts,
        }],
        "tool_history": [{"tool_name": "read_inventory_risk_run", "status": "completed"}],
    }


def read_forecast_run(
    engine: Engine,
    *,
    run_id: str,
    evaluation_period: Literal["rolling_origin", "held_out"] = "held_out",
) -> InvestigationState:
    """Return saved forecast evidence through the same approved read path as the graph."""
    return _read_forecast(
        {"run_id": run_id, "evaluation_period": evaluation_period},
        engine,
    )


def read_inventory_risk_run(engine: Engine, *, run_id: str) -> InvestigationState:
    """Return saved inventory-risk evidence through the approved read path."""
    return _read_inventory_risk({"run_id": run_id}, engine)


def _validate_evidence(state: InvestigationState) -> InvestigationState:
    """Reject non-finite metrics and evidence references that do not match the request."""
    evidence = state.get("evidence", [])
    references_valid = bool(evidence) and all(
        item.get("source_run_id") == state.get("run_id")
        and isinstance(item.get("evidence_id"), str)
        and bool(item["evidence_id"])
        for item in evidence
    )
    numbers: list[float] = []
    for item in evidence:
        facts = item.get("facts", {})
        if state.get("evidence_type") == "forecast":
            metrics = facts.get("metrics")
            if not isinstance(metrics, list):
                numbers.append(math.nan)
                metrics = []
            for metric in metrics:
                if not isinstance(metric, dict):
                    numbers.append(math.nan)
                    continue
                for key in ("mae", "wape", "bias"):
                    value = metric.get(key)
                    if value is None and key != "mae":
                        continue
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                        or (key in {"mae", "wape"} and value < 0)
                    ):
                        numbers.append(math.nan)
                    else:
                        numbers.append(value)
                observations = metric.get("observations")
                if (
                    isinstance(observations, bool)
                    or not isinstance(observations, int)
                    or observations <= 0
                ):
                    numbers.append(math.nan)
        else:
            for key, value_key in (
                ("highest_stockout", "stockout_probability"),
                ("highest_expiry", "expiry_units_at_risk"),
            ):
                row = facts.get(key)
                if not isinstance(row, dict):
                    numbers.append(math.nan)
                else:
                    numbers.append(row.get(value_key, math.nan))
            if not isinstance(facts.get("series_count"), int) or facts["series_count"] <= 0:
                numbers.append(math.nan)
            service_level = facts.get("service_level")
            if not isinstance(service_level, (int, float)) or not 0 < service_level < 1:
                numbers.append(math.nan)
            expiry_row = facts.get("highest_expiry")
            expiry_units = expiry_row.get("expiry_units_at_risk") if isinstance(expiry_row, dict) else None
            if not isinstance(expiry_units, (int, float)) or expiry_units < 0:
                numbers.append(math.nan)
    numeric_valid = bool(evidence) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in numbers
    )
    if state.get("evidence_type") == "inventory-risk" and numeric_valid:
        risk_facts = evidence[0]["facts"]
        stockout_row = risk_facts.get("highest_stockout")
        probability = stockout_row.get("stockout_probability") if isinstance(stockout_row, dict) else None
        numeric_valid = isinstance(probability, (int, float)) and 0 <= probability <= 1
    return {
        "schema_valid": bool(evidence) and all(isinstance(item.get("facts"), dict) for item in evidence),
        "numeric_claims_valid": numeric_valid,
        "evidence_references_valid": references_valid,
    }


def _build_template_answer(state: InvestigationState) -> InvestigationState:
    """Create a readable answer only from validated database evidence."""
    if state.get("status") == "not_found":
        return {"answer": f"No saved {state['evidence_type']} run was found for ID {state['run_id']}."}
    valid = (
        state.get("schema_valid")
        and state.get("numeric_claims_valid")
        and state.get("evidence_references_valid")
    )
    if not valid:
        return {
            "status": "validation_failed",
            "answer": "The saved evidence did not pass validation, so no numeric summary was produced.",
        }

    facts = state["evidence"][0]["facts"]
    if state["evidence_type"] == "forecast":
        metrics = facts["metrics"]
        if not metrics:
            return {"answer": f"Forecast run {state['run_id']} has no metrics for {state['evaluation_period']}."}
        best = metrics[0]
        wape_text = "not recorded" if best["wape"] is None else f"{best['wape']:.4f}"
        bias_text = "not recorded" if best["bias"] is None else f"{best['bias']:.4f}"
        answer = (
            f"For {facts['state_id']}/{facts['category_id']} across {facts['series_count']} series, "
            f"{best['model_name']} has the lowest saved MAE in {facts['evaluation_period']} "
            f"({best['mae']:.4f}; WAPE {wape_text}; bias {bias_text}; "
            f"{best['observations']} observations). These are M5 observed-sales evaluation metrics."
        )
    else:
        stockout = facts["highest_stockout"]
        expiry = facts["highest_expiry"]
        answer = (
            f"Risk run {state['run_id']} covers {facts['series_count']} series at a "
            f"{facts['service_level']:.0%} service-level target. Highest recorded stockout probability: "
            f"{stockout['stockout_probability']:.4f} for {stockout['series_id']}. "
            f"Highest recorded expiry exposure: {expiry['expiry_units_at_risk']:.2f} units "
            f"for {expiry['series_id']}. Inventory values are {facts['inventory_provenance']}; "
            f"demand reference: {facts['demand_reference']}."
        )
    return {"answer": answer}


def _explain_with_ollama(
    state: InvestigationState,
    settings: Settings,
) -> InvestigationState:
    """Add only a validated qualitative note; preserve deterministic summary on any failure."""
    model_name = settings.ollama_model if settings.llm_enabled else None
    base = {
        "explanation_source": "deterministic_template",
        "provider": "ollama" if settings.llm_enabled else "none",
        "model_name": model_name,
        "llm_used": False,
    }
    if state.get("status") != "completed":
        return {**base, "llm_fallback_reason": "no_validated_evidence"}
    if not all(
        state.get(key)
        for key in ("schema_valid", "numeric_claims_valid", "evidence_references_valid")
    ):
        return {**base, "llm_fallback_reason": "evidence_validation_failed"}

    evidence = state["evidence"][0]
    outcome = generate_explanation(
        settings,
        question=state.get("question"),
        evidence_facts=evidence["facts"],
    )
    if outcome.text is None:
        return {**base, "llm_fallback_reason": outcome.fallback_reason}
    answer = state["answer"] + "\n\nAI interpretation: " + outcome.text
    return {
        **base,
        "answer": answer,
        "explanation_source": "deterministic_template+ollama",
        "model_name": outcome.model_name,
        "llm_used": True,
        "llm_fallback_reason": None,
    }


_DOMAIN_AGENT: dict[str, AgentName] = {
    "forecast": "forecast_intelligence",
    "inventory-risk": "inventory_risk",
    "decision": "decision_intelligence",
}


def _latest_run_id(engine: Engine, domain: str) -> str | None:
    model = ForecastRun if domain == "forecast" else InventoryRiskRun
    with Session(engine) as session:
        run = session.scalars(
            select(model).order_by(model.generated_at_utc.desc()).limit(1)
        ).first()
        return run.run_id if run is not None else None


def _prepare_request(state: InvestigationState, settings: Settings) -> InvestigationState:
    """Preflight Ollama once, then classify to a bounded set of evidence domains."""
    if settings.llm_enabled:
        model_available, preflight_failure = check_model_available(settings)
    else:
        model_available, preflight_failure = False, "disabled"
    classified = classify_request(
        settings=settings,
        model_available=model_available,
        model_failure=preflight_failure,
        question=state.get("question"),
        legacy_evidence_type=state.get("evidence_type"),
        explicit_domains=state.get("requested_domains"),
        include_business_intelligence=bool(state.get("include_business_intelligence")),
    )
    route = classified.get("route")
    if not isinstance(route, dict):
        return {
            **classified,
            "route": None,
            "status": "needs_clarification",
            "answer": (
                "I could not safely determine which saved evidence to inspect. "
                "Please specify forecast, inventory risk, or a decision scenario."
            ),
            "model_available": model_available,
            "preflight_failure": preflight_failure,
            "agent_results": [],
        }

    domains = list(dict.fromkeys(route.get("domains", [])))
    decision_scenarios = list(
        state.get("decision_scenarios") or route.get("decision_scenarios", [])
    )
    selected_agents = [_DOMAIN_AGENT[domain] for domain in domains]
    if route.get("include_business_intelligence"):
        selected_agents.append("business_intelligence")
    run_ids = dict(state.get("requested_run_ids", {}))
    if state.get("evidence_type") and state.get("run_id"):
        run_ids[state["evidence_type"]] = state["run_id"]
    fallback = classified.get("route_failure")
    if settings.llm_enabled and not model_available:
        fallback = fallback or preflight_failure or "model_unavailable"
    return {
        **classified,
        "route": route,
        "domains": domains,
        "run_ids": run_ids,
        "decision_scenarios": decision_scenarios,
        "selected_agents": selected_agents,
        "include_business_intelligence": bool(route.get("include_business_intelligence")),
        "model_available": model_available,
        "preflight_failure": None if model_available else preflight_failure,
        "model_name": classified.get("model_name") or (
            settings.ollama_model if settings.llm_enabled else None
        ),
        "status": "pending",
        "llm_fallback_reason": fallback,
        "agent_results": [],
    }


def _read_selected_evidence(state: InvestigationState, engine: Engine) -> InvestigationState:
    """Read the approved database/report sources selected by the supervisor."""
    evidence: list[dict[str, Any]] = []
    history: list[dict[str, str]] = []
    run_ids = dict(state.get("run_ids", {}))
    database_retrieval_seconds = 0.0
    for domain in state.get("domains", []):
        if domain in {"forecast", "inventory-risk"}:
            database_started = perf_counter()
            run_id = run_ids.get(domain) or _latest_run_id(engine, domain)
            if not run_id:
                database_retrieval_seconds += perf_counter() - database_started
                history.append({"tool_name": f"read_{domain}_run", "status": "not_found"})
                continue
            run_ids[domain] = run_id
            if domain == "forecast":
                result = read_forecast_run(
                    engine,
                    run_id=run_id,
                    evaluation_period=state.get("evaluation_period", "held_out"),
                )
            else:
                result = read_inventory_risk_run(engine, run_id=run_id)
            database_retrieval_seconds += perf_counter() - database_started
            for item in result.get("evidence", []):
                evidence.append({"domain": domain, **item})
            history.extend(result.get("tool_history", []))
        elif domain == "decision":
            try:
                reports = read_latest_decision_reports(
                    PROJECT_ROOT,
                    state.get("decision_scenarios") or None,
                    run_ids.get("decision"),
                )
                evidence.extend(reports)
                history.extend(
                    {"tool_name": f"read_{item['facts']['scenario']}_report", "status": "completed"}
                    for item in reports
                )
                if not reports:
                    history.append({"tool_name": "read_decision_reports", "status": "not_found"})
            except DecisionEvidenceError:
                history.append({"tool_name": "read_decision_reports", "status": "failed"})

    resolved_ids = {
        item["domain"]: item["source_run_id"]
        for item in evidence
        if item.get("domain") and item.get("source_run_id")
    }
    status = "completed" if evidence else "not_found"
    available_domains = {item.get("domain") for item in evidence}
    effective_agents: list[AgentName] = [
        _DOMAIN_AGENT[domain] for domain in state.get("domains", []) if domain in available_domains
    ]
    if (
        state.get("include_business_intelligence")
        and len(available_domains) > 1
    ):
        effective_agents.append("business_intelligence")
    return {
        "evidence": evidence,
        "tool_history": history,
        "run_ids": resolved_ids,
        "selected_agents": effective_agents,
        "status": status,
        "database_retrieval_duration_ms": max(0, int(database_retrieval_seconds * 1000)),
    }


def _call_agent(
    agent: AgentName,
    state: InvestigationState,
    settings: Settings,
    *,
    prior_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if agent == "business_intelligence":
        agent_evidence = state.get("evidence", [])
    else:
        domain = next(domain for domain, name in _DOMAIN_AGENT.items() if name == agent)
        agent_evidence = [item for item in state.get("evidence", []) if item.get("domain") == domain]
    agent_functions = {
        "forecast_intelligence": run_forecast_agent,
        "inventory_risk": run_inventory_risk_agent,
        "decision_intelligence": run_decision_agent,
        "business_intelligence": run_business_intelligence_agent,
    }
    return agent_functions[agent](
        question=state.get("question"),
        evidence=agent_evidence,
        settings=settings,
        prior_findings=prior_findings,
        preflight_failure=state.get("preflight_failure"),
    )


def _run_one_specialist(state: InvestigationState, settings: Settings) -> InvestigationState:
    result = _call_agent(state["agent_name"], state, settings)
    return {"agent_results": [result]}


def _run_specialists_sequentially(
    state: InvestigationState,
    settings: Settings,
) -> InvestigationState:
    results = [
        _call_agent(agent, state, settings)
        for agent in state.get("selected_agents", [])
        if agent != "business_intelligence"
    ]
    return {"agent_results": results}


def _run_business_intelligence(state: InvestigationState, settings: Settings) -> InvestigationState:
    """Synthesize completed specialist outputs once, after their graph barrier."""
    result = _call_agent(
        "business_intelligence",
        state,
        settings,
        prior_findings=state.get("agent_results", []),
    )
    return {"agent_results": [result]}


def _dispatch_specialists(state: InvestigationState, execution_mode: str) -> list[Send] | str:
    if state.get("status") != "completed":
        return "validate_state"
    selected = [agent for agent in state.get("selected_agents", []) if agent != "business_intelligence"]
    if not selected:
        return "validate_state"
    if execution_mode == "parallel":
        return [Send("run_one_specialist", {**state, "agent_name": agent}) for agent in selected]
    return "run_specialists_sequentially"


def _validate_state(state: InvestigationState) -> InvestigationState:
    """Validate saved numeric data and every accepted specialist citation."""
    evidence = state.get("evidence", [])
    expected_run_ids = state.get("run_ids", {})
    evidence_ids = [item.get("evidence_id") for item in evidence if isinstance(item, dict)]
    references_valid = bool(evidence) and len(evidence_ids) == len(set(evidence_ids)) and all(
        isinstance(item.get("evidence_id"), str)
        and isinstance(item.get("source_run_id"), str)
        and bool(item["source_run_id"])
        and item.get("domain") in state.get("domains", [])
        and isinstance(item.get("facts"), dict)
        and (
            item.get("domain") == "decision"
            or item.get("source_run_id") == expected_run_ids.get(item.get("domain"))
        )
        and (
            item.get("domain") != "decision"
            or item.get("facts", {}).get("run_id") == item.get("source_run_id")
        )
        for item in evidence
    )

    def finite_tree(value: Any) -> bool:
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return True
        if isinstance(value, (int, float)):
            try:
                return math.isfinite(float(value))
            except (OverflowError, ValueError):
                return False
        if isinstance(value, dict):
            return all(isinstance(key, str) and finite_tree(item) for key, item in value.items())
        if isinstance(value, list):
            return all(finite_tree(item) for item in value)
        return False

    numeric_valid = bool(evidence) and all(finite_tree(item.get("facts")) for item in evidence)
    for item in evidence:
        facts = item.get("facts", {})
        domain = item.get("domain")
        if domain == "forecast":
            metrics = facts.get("metrics")
            numeric_valid = numeric_valid and isinstance(metrics, list) and bool(metrics)
            for metric in metrics if isinstance(metrics, list) else []:
                if not isinstance(metric, dict):
                    numeric_valid = False
                    continue
                for key in ("mae", "wape", "bias"):
                    value = metric.get(key)
                    if value is None and key != "mae":
                        continue
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or (key in {"mae", "wape"} and value < 0)
                    ):
                        numeric_valid = False
                observations = metric.get("observations")
                if isinstance(observations, bool) or not isinstance(observations, int) or observations <= 0:
                    numeric_valid = False
        elif domain == "inventory-risk":
            stockout = facts.get("highest_stockout")
            expiry = facts.get("highest_expiry")
            probability = stockout.get("stockout_probability") if isinstance(stockout, dict) else None
            expiry_units = expiry.get("expiry_units_at_risk") if isinstance(expiry, dict) else None
            service_level = facts.get("service_level")
            series_count = facts.get("series_count")
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not 0 <= probability <= 1
                or isinstance(expiry_units, bool)
                or not isinstance(expiry_units, (int, float))
                or expiry_units < 0
                or isinstance(service_level, bool)
                or not isinstance(service_level, (int, float))
                or not 0 < service_level < 1
                or isinstance(series_count, bool)
                or not isinstance(series_count, int)
                or series_count < 1
            ):
                numeric_valid = False
        elif domain == "decision":
            numeric_valid = numeric_valid and facts.get("scenario") in {
                "markdown", "purchase_optimization", "policy_benchmark"
            }
            numeric_valid = numeric_valid and facts.get("status") in {
                "completed", "optimal", "feasible", "infeasible", "unknown"
            }
            scenario = facts.get("scenario")
            if scenario == "purchase_optimization":
                optimization = facts.get("optimization")
                if not isinstance(optimization, dict):
                    numeric_valid = False
                else:
                    amounts = [optimization.get(key) for key in (
                        "budget", "total_spend", "remaining_budget"
                    )]
                    if not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                        and value >= 0
                        for value in amounts
                    ):
                        numeric_valid = False
                    else:
                        budget, spend, remaining = amounts
                        numeric_valid = numeric_valid and spend <= budget + 0.01 and abs(
                            budget - spend - remaining
                        ) <= 0.02
            elif scenario == "policy_benchmark":
                benchmark = facts.get("benchmark")
                policies = benchmark.get("policies") if isinstance(benchmark, dict) else None
                if not isinstance(policies, list):
                    numeric_valid = False
                for policy in policies if isinstance(policies, list) else []:
                    if not isinstance(policy, dict):
                        numeric_valid = False
                        continue
                    covered = policy.get("covered_exposure_units")
                    total = policy.get("total_exposure_units")
                    cost = policy.get("estimated_order_cost")
                    if not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                        for value in (covered, total, cost)
                    ) or covered < 0 or total < 0 or covered > total or cost < 0:
                        numeric_valid = False
    allowed_ids = {item.get("evidence_id") for item in evidence}
    agent_output_valid = True
    evidence_by_id = {
        item.get("evidence_id"): item for item in evidence if isinstance(item, dict)
    }

    def resolve_fact(evidence_item: dict[str, Any], parts: list[Any]) -> Any:
        value: Any = evidence_item.get("facts")
        for part in parts:
            if isinstance(part, bool) or not isinstance(part, (str, int)) or (isinstance(part, int) and part < 0):
                raise KeyError("invalid evidence path")
            value = value[part]
        return value

    def fact_path_text(parts: list[Any]) -> str:
        result = "facts"
        for part in parts:
            result += f"[{part}]" if isinstance(part, int) else f".{part}"
        return result

    for result in state.get("agent_results", []):
        if result.get("status") != "completed":
            continue
        if not result.get("numeric_claims_valid") or not result.get("evidence_refs_valid"):
            agent_output_valid = False
        if contains_numeric_claim(result.get("summary", "")):
            agent_output_valid = False
        prose_fields = [
            *result.get("assumptions", []),
            *result.get("missing_data", []),
            *result.get("limitations", []),
            result.get("confidence_basis", ""),
        ]
        if any(contains_numeric_claim(text) for text in prose_fields):
            agent_output_valid = False
        is_decision = result.get("agent") == "decision_intelligence"
        if is_decision:
            decision_prose = [
                result.get("decision_summary", ""),
                result.get("explanation", ""),
                *result.get("constraints", []),
            ]
            if any(contains_numeric_claim(text) for text in decision_prose):
                agent_output_valid = False
            decision_evidence_refs = result.get("decision_evidence_refs", [])
            if (
                not isinstance(decision_evidence_refs, list)
                or not decision_evidence_refs
                or not set(decision_evidence_refs).issubset(allowed_ids)
            ):
                agent_output_valid = False
            seen_fact_refs: set[str] = set()
            numeric_findings = result.get("verified_numerical_findings", [])
            if not isinstance(numeric_findings, list) or not numeric_findings:
                agent_output_valid = False
            for finding in numeric_findings if isinstance(numeric_findings, list) else []:
                if not isinstance(finding, dict):
                    agent_output_valid = False
                    continue
                evidence_item = evidence_by_id.get(finding.get("evidence_id"))
                fact_ref = finding.get("fact_ref")
                path_parts = finding.get("source_path_parts")
                value = finding.get("value")
                try:
                    source_value = resolve_fact(evidence_item, path_parts) if evidence_item and isinstance(path_parts, list) else None
                except (KeyError, IndexError, TypeError):
                    source_value = None
                if (
                    not isinstance(fact_ref, str)
                    or fact_ref in seen_fact_refs
                    or evidence_item is None
                    or evidence_item.get("domain") != "decision"
                    or evidence_item.get("source_run_id") != finding.get("source_run_id")
                    or finding.get("evidence_id") not in decision_evidence_refs
                    or not isinstance(finding.get("label"), str)
                    or not finding.get("label")
                    or not isinstance(finding.get("source_path"), str)
                    or not finding.get("source_path")
                    or not isinstance(path_parts, list)
                    or finding.get("source_path") != fact_path_text(path_parts)
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or source_value != value
                ):
                    agent_output_valid = False
                if isinstance(fact_ref, str):
                    seen_fact_refs.add(fact_ref)
        for finding in result.get("findings", []):
            if contains_numeric_claim(finding.get("text", "")) or not set(
                finding.get("evidence_refs", [])
            ).issubset(allowed_ids):
                agent_output_valid = False
    return {
        "schema_valid": bool(evidence) and all(
            isinstance(item, dict) and isinstance(item.get("facts"), dict) for item in evidence
        ),
        "numeric_claims_valid": numeric_valid and agent_output_valid,
        "evidence_references_valid": references_valid,
        "agent_output_valid": agent_output_valid,
    }


def _validate_evidence_before_agents(state: InvestigationState) -> InvestigationState:
    """Do not send saved facts to an LLM until the source packet passes validation."""
    validation = _validate_state(state)
    valid = all(
        validation.get(field) is True
        for field in ("schema_valid", "numeric_claims_valid", "evidence_references_valid")
    )
    if state.get("status") == "not_found":
        return {**validation, "status": "not_found"}
    return {**validation, "status": "completed" if valid else "validation_failed"}


def _render_final_answer(state: InvestigationState) -> InvestigationState:
    """Keep numeric statements deterministic; append only validated qualitative agent prose."""
    if state.get("status") == "needs_clarification":
        return _final_metadata(state)
    if state.get("status") == "not_found":
        domains = state.get("domains", [])
        if len(domains) == 1 and domains[0] != "decision":
            run_id = state.get("run_ids", {}).get(domains[0])
            label = "forecast" if domains[0] == "forecast" else "inventory-risk"
            identifier = f" with ID {run_id}" if run_id else ""
            return {
                "answer": f"No saved {label} run{identifier} was found.",
                **_final_metadata(state),
            }
        return {
            "answer": "No matching saved evidence was found for this investigation.",
            **_final_metadata(state),
        }
    valid = all(
        state.get(field)
        for field in ("schema_valid", "numeric_claims_valid", "evidence_references_valid", "agent_output_valid")
    )
    if not valid:
        return {
            "status": "validation_failed",
            "answer": "The saved evidence or specialist output failed validation; no numeric summary was produced.",
            **_final_metadata(state),
        }

    summaries: list[str] = []
    for domain in state.get("domains", []):
        domain_evidence = [item for item in state.get("evidence", []) if item.get("domain") == domain]
        if not domain_evidence:
            continue
        if domain in {"forecast", "inventory-risk"}:
            for item in domain_evidence:
                local_state: InvestigationState = {
                    "evidence_type": domain,
                    "run_id": item["source_run_id"],
                    "evaluation_period": state.get("evaluation_period", "held_out"),
                    "evidence": [item],
                    "status": "completed",
                    "schema_valid": True,
                    "numeric_claims_valid": True,
                    "evidence_references_valid": True,
                }
                summaries.append(_build_template_answer(local_state)["answer"])
        else:
            for item in domain_evidence:
                facts = item["facts"]
                scenario = facts["scenario"]
                if scenario == "purchase_optimization":
                    optimization = facts.get("optimization") or {}
                    selection = facts.get("selection") or {}
                    series_count = selection.get("series_count")
                    series_count_text = (
                        str(series_count)
                        if isinstance(series_count, int) and not isinstance(series_count, bool) and series_count > 0
                        else "an unspecified number of"
                    )
                    if all(
                        isinstance(optimization.get(key), (int, float))
                        and not isinstance(optimization.get(key), bool)
                        and math.isfinite(float(optimization[key]))
                        for key in ("budget", "total_spend", "remaining_budget")
                    ):
                        summaries.append(
                            f"Saved purchase optimization {item['source_run_id']} is {optimization.get('status', facts['status'])}: "
                            f"budget {optimization['budget']:.2f}, estimated spend {optimization['total_spend']:.2f}, "
                            f"remaining budget {optimization['remaining_budget']:.2f}; "
                            f"the selected scope contains {series_count_text} series. "
                            "It is a simulated recommendation and does not submit an order."
                        )
                    else:
                        summaries.append(
                            f"Saved purchase optimization {item['source_run_id']} has status {facts['status']}; "
                            "it is a simulated recommendation and does not submit an order."
                        )
                elif scenario == "policy_benchmark":
                    benchmark = facts.get("benchmark") or {}
                    policy_rows = benchmark.get("policies", [])
                    policy_summaries = []
                    for policy in policy_rows:
                        if not isinstance(policy, dict):
                            continue
                        covered = policy.get("covered_exposure_units")
                        total = policy.get("total_exposure_units")
                        cost = policy.get("estimated_order_cost")
                        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(float(value)) for value in (covered, total, cost)):
                            if covered < 0 or total < 0 or covered > total or cost < 0:
                                continue
                            policy_summaries.append(
                                f"{policy.get('policy_name', policy.get('policy_id', 'Policy'))}: "
                                f"covered exposure {covered:g}/{total:g}, estimated cost {cost:.2f}"
                            )
                    summary = "; ".join(policy_summaries) or "No valid policy metrics were recorded."
                    summaries.append(
                        f"Saved policy benchmark {item['source_run_id']} ({facts['status']}): {summary}. "
                        "These are simulated scenario comparisons, not measured service outcomes or savings."
                    )
                else:
                    selection = facts.get("selection") or {}
                    series_count = selection.get("series_count")
                    series_count_text = (
                        str(series_count)
                        if isinstance(series_count, int) and not isinstance(series_count, bool) and series_count > 0
                        else "an unspecified number of"
                    )
                    summaries.append(
                        f"Saved markdown report {item['source_run_id']} covers "
                        f"{series_count_text} selected series. The demand-response formula is a stated simulation assumption. "
                        "This is a simulated pricing scenario; it does not change live prices."
                    )

    if not summaries:
        summaries.append("Validated saved evidence is available for the selected investigation.")
    accepted_agents = [
        result for result in state.get("agent_results", []) if result.get("status") == "completed"
    ]
    for result in accepted_agents:
        summaries.append(f"{result['agent'].replace('_', ' ').title()}: {result['summary']}")
        summaries.extend(f"Finding: {finding['text']}" for finding in result.get("findings", []))
        if result.get("agent") == "decision_intelligence":
            details = result.get("decision_summary")
            if isinstance(details, str) and details and details != result["summary"]:
                summaries.append(f"Decision summary: {details}")
            explanation = result.get("explanation")
            if isinstance(explanation, str) and explanation:
                summaries.append(f"Decision interpretation: {explanation}")
            verified_findings = result.get("verified_numerical_findings", [])
            for finding in verified_findings:
                unit = f" {finding['unit']}" if finding.get("unit") else ""
                summaries.append(
                    f"Verified decision finding — {finding['label']}: {finding['value']}{unit} "
                    f"(saved evidence {finding['evidence_id']}, {finding['source_path']})."
                )
            for field_name in ("constraints", "assumptions", "limitations"):
                for note in result.get(field_name, []):
                    summaries.append(f"Decision {field_name.removesuffix('s')}: {note}")
    return {"answer": "\n\n".join(summaries), **_final_metadata(state)}


def _final_metadata(state: InvestigationState) -> dict[str, Any]:
    results = state.get("agent_results", [])
    failures = [result.get("failure_reason") for result in results if result.get("failure_reason")]
    if state.get("route_failure"):
        failures.insert(0, state["route_failure"])
    if state.get("preflight_failure") and state.get("preflight_failure") != "disabled":
        failures.insert(0, state["preflight_failure"])
    unique_failures = list(dict.fromkeys(str(reason) for reason in failures))
    classifier_called = bool(state.get("classifier_called", False))
    ollama_call_count = int(classifier_called) + sum(
        int(bool(result.get("call_made"))) for result in results
    )
    llm_inference_duration_ms = int(state.get("classifier_duration_ms", 0)) + sum(
        int(result.get("duration_ms", 0)) for result in results
    )
    llm_used = any(result.get("status") == "completed" and result.get("call_made") for result in results)
    used_domains = state.get("domains", [])
    source_run_ids: dict[str, list[str]] = {}
    for item in state.get("evidence", []):
        domain = item.get("domain")
        source_id = item.get("source_run_id")
        if isinstance(domain, str) and isinstance(source_id, str):
            source_run_ids.setdefault(domain, []).append(source_id)
    evidence_type = (
        used_domains[0] if len(used_domains) == 1 else "multi-domain" if used_domains else "business"
    )
    return {
        "evidence_type": evidence_type,
        "selected_domains": used_domains,
        "source_run_ids": source_run_ids,
        "selected_agents": state.get("selected_agents", []),
        "llm_used": llm_used,
        "explanation_source": "deterministic_template+ollama_agents" if llm_used else "deterministic_template",
        "provider": "ollama" if ollama_call_count else "none",
        "model_name": state.get("model_name") if ollama_call_count else None,
        "ollama_call_count": ollama_call_count,
        "llm_inference_duration_ms": llm_inference_duration_ms,
        "database_retrieval_duration_ms": int(state.get("database_retrieval_duration_ms", 0)),
        "llm_fallback_reason": ",".join(unique_failures)[:200] or None,
        "schema_valid": state.get("schema_valid", False),
        "numeric_claims_valid": state.get("numeric_claims_valid", False),
        "evidence_references_valid": state.get("evidence_references_valid", False),
        "agent_output_valid": state.get("agent_output_valid", True),
    }


def build_investigation_graph(
    engine: Engine,
    checkpointer: BaseCheckpointSaver,
    settings: Settings | None = None,
):
    """Build the local-only supervisor graph with selected specialist branches."""
    settings = settings or Settings(database_url=None)
    builder = StateGraph(InvestigationState)
    builder.add_node("prepare_request", lambda state: _prepare_request(state, settings))
    builder.add_node("read_selected_evidence", lambda state: _read_selected_evidence(state, engine))
    builder.add_node("run_one_specialist", lambda state: _run_one_specialist(state, settings))
    builder.add_node("run_specialists_sequentially", lambda state: _run_specialists_sequentially(state, settings))
    builder.add_node(
        "run_business_intelligence",
        lambda state: _run_business_intelligence(state, settings),
    )
    builder.add_node("validate_evidence", _validate_evidence_before_agents)
    builder.add_node("validate_state", _validate_state)
    builder.add_node("render_final_answer", _render_final_answer)
    builder.add_node("after_specialists", lambda state: {})
    builder.add_edge(START, "prepare_request")
    builder.add_conditional_edges(
        "prepare_request",
        lambda state: "read_selected_evidence" if state.get("route") else "render_final_answer",
        {"read_selected_evidence": "read_selected_evidence", "render_final_answer": "render_final_answer"},
    )
    builder.add_edge("read_selected_evidence", "validate_evidence")
    builder.add_conditional_edges(
        "validate_evidence",
        lambda state: (
            "render_final_answer"
            if state.get("status") != "completed"
            else _dispatch_specialists(state, settings.agent_execution_mode)
        ),
        {
            "run_specialists_sequentially": "run_specialists_sequentially",
            "run_one_specialist": "run_one_specialist",
            "validate_state": "validate_state",
            "render_final_answer": "render_final_answer",
        },
    )
    builder.add_edge("run_one_specialist", "after_specialists")
    builder.add_edge("run_specialists_sequentially", "after_specialists")
    builder.add_conditional_edges(
        "after_specialists",
        lambda state: "run_business_intelligence"
        if "business_intelligence" in state.get("selected_agents", [])
        else "validate_state",
        {"run_business_intelligence": "run_business_intelligence", "validate_state": "validate_state"},
    )
    builder.add_edge("run_business_intelligence", "validate_state")
    builder.add_edge("validate_state", "render_final_answer")
    builder.add_edge("render_final_answer", END)
    return builder.compile(checkpointer=checkpointer)


def run_investigation(
    engine: Engine,
    *,
    evidence_type: EvidenceType | None = None,
    run_id: str | None = None,
    evaluation_period: Literal["rolling_origin", "held_out"] = "held_out",
    checkpointer: BaseCheckpointSaver,
    question: str | None = None,
    domains: list[EvidenceDomain] | None = None,
    run_ids: dict[str, str] | None = None,
    decision_scenarios: list[DecisionScenario] | None = None,
    include_business_intelligence: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run and checkpoint a bounded investigation under an opaque thread ID."""
    settings = settings or Settings(database_url=None)
    investigation_id = uuid.uuid4().hex
    graph = build_investigation_graph(engine, checkpointer, settings)
    state = graph.invoke(
        {
            "evidence_type": evidence_type,
            "run_id": run_id,
            "evaluation_period": evaluation_period,
            "question": question,
            "requested_domains": domains,
            "requested_run_ids": run_ids or {},
            "decision_scenarios": decision_scenarios or [],
            "include_business_intelligence": include_business_intelligence,
            "evidence": [],
            "tool_history": [],
            "agent_results": [],
        },
        {
            "configurable": {"thread_id": investigation_id},
            "recursion_limit": 12,
        },
    )
    return {"investigation_id": investigation_id, **state}


def load_investigation_checkpoint(
    engine: Engine,
    checkpointer: BaseCheckpointSaver,
    investigation_id: str,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Load a completed graph state by its opaque investigation/thread ID."""
    graph = build_investigation_graph(engine, checkpointer, settings)
    snapshot = graph.get_state({"configurable": {"thread_id": investigation_id}})
    if not snapshot.values:
        return None
    return {"investigation_id": investigation_id, **snapshot.values}
