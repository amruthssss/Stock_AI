"""Run a fixed, deterministic evaluation set against persisted evidence readers."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from backend.agent.investigation import run_investigation
from backend.api.config import get_settings
from backend.api.database import create_database_engine
from backend.persistence.models import ForecastRun, InventoryRiskRun


QUESTIONS_PATH = Path(__file__).with_name("evaluation_questions.json")


class EvaluationSetError(ValueError):
    """The curated question set is missing or malformed."""


def load_evaluation_questions(path: Path = QUESTIONS_PATH) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationSetError("Agent evaluation question file could not be read") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
        raise EvaluationSetError("Agent evaluation file must contain a questions list")
    questions = payload["questions"]
    if len(questions) < 40 or payload.get("case_count") != len(questions):
        raise EvaluationSetError("Agent evaluation set must declare at least 40 matching cases")
    seen_ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise EvaluationSetError("Agent evaluation cases must be objects")
        case_id = question.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise EvaluationSetError("Agent evaluation case IDs must be unique non-empty strings")
        seen_ids.add(case_id)
        if question.get("evidence_type") not in {"forecast", "inventory-risk"}:
            raise EvaluationSetError(f"Agent evaluation case {case_id} has an unsupported evidence type")
        if not isinstance(question.get("question"), str) or not question["question"].strip():
            raise EvaluationSetError(f"Agent evaluation case {case_id} has no question text")
        markers = question.get("required_answer_markers")
        if not isinstance(markers, list) or not markers or not all(
            isinstance(marker, str) and marker for marker in markers
        ):
            raise EvaluationSetError(f"Agent evaluation case {case_id} has invalid answer markers")
        if question["evidence_type"] == "forecast" and question.get("evaluation_period") not in {
            "held_out",
            "rolling_origin",
        }:
            raise EvaluationSetError(f"Agent evaluation case {case_id} has an invalid evaluation period")
    return questions


def evaluate_agent_questions(
    engine: Engine,
    *,
    forecast_run_id: str,
    inventory_risk_run_id: str,
    questions_path: Path = QUESTIONS_PATH,
) -> dict[str, Any]:
    """Run the curated pre-routed cases and check evidence, validation, and safe wording."""
    questions = load_evaluation_questions(questions_path)
    checkpointer = InMemorySaver()
    results: list[dict[str, Any]] = []
    for case in questions:
        evidence_type = case["evidence_type"]
        run_id = forecast_run_id if evidence_type == "forecast" else inventory_risk_run_id
        state = run_investigation(
            engine,
            evidence_type=evidence_type,
            run_id=run_id,
            evaluation_period=case.get("evaluation_period", "held_out"),
            question=case["question"],
            checkpointer=checkpointer,
        )
        expected_tool = "read_forecast_run" if evidence_type == "forecast" else "read_inventory_risk_run"
        evidence = state.get("evidence", [])
        facts = evidence[0].get("facts", {}) if evidence else {}
        markers_present = all(marker in state.get("answer", "") for marker in case["required_answer_markers"])
        checks = {
            "completed": state.get("status") == "completed",
            "schema_valid": state.get("schema_valid") is True,
            "numeric_claims_valid": state.get("numeric_claims_valid") is True,
            "evidence_references_valid": state.get("evidence_references_valid") is True,
            "correct_run_reference": bool(evidence) and evidence[0].get("source_run_id") == run_id,
            "approved_tool_used": bool(state.get("tool_history"))
            and state["tool_history"][0].get("tool_name") == expected_tool
            and state["tool_history"][0].get("status") == "completed",
            "answer_markers_present": markers_present,
            "llm_disabled": state.get("llm_used") is False,
        }
        results.append(
            {
                "case_id": case["id"],
                "question": case["question"],
                "evidence_type": evidence_type,
                "evaluation_period": case.get("evaluation_period"),
                "source_run_id": run_id,
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
                "answer": state.get("answer", ""),
                "evidence_facts": facts,
            }
        )
    passed = sum(result["status"] == "passed" for result in results)
    return {
        "evaluation_id": uuid.uuid4().hex,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if passed == len(results) else "failed",
        "scope": "pre-routed evidence retrieval and deterministic summary safety; not intent routing or LLM quality",
        "llm_used": False,
        "forecast_run_id": forecast_run_id,
        "inventory_risk_run_id": inventory_risk_run_id,
        "case_count": len(results),
        "passed_count": passed,
        "failed_count": len(results) - passed,
        "cases": results,
    }


def _latest_run_ids(engine: Engine) -> tuple[str, str]:
    with Session(engine) as session:
        forecast_run_id = session.scalar(
            select(ForecastRun.run_id).order_by(ForecastRun.generated_at_utc.desc()).limit(1)
        )
        risk_run_id = session.scalar(
            select(InventoryRiskRun.run_id).order_by(InventoryRiskRun.generated_at_utc.desc()).limit(1)
        )
    if forecast_run_id is None or risk_run_id is None:
        raise EvaluationSetError("A completed forecast run and inventory-risk run must be imported first")
    return forecast_run_id, risk_run_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate StockWise evidence responses against 40 curated cases.")
    parser.add_argument("--forecast-run-id")
    parser.add_argument("--inventory-risk-run-id")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    arguments = parser.parse_args()
    settings = get_settings(arguments.project_root)
    if not settings.database_url:
        parser.error("STOCKWISE_DATABASE_URL must be configured")
    engine = create_database_engine(settings.database_url)
    try:
        latest_forecast, latest_risk = _latest_run_ids(engine)
        result = evaluate_agent_questions(
            engine,
            forecast_run_id=arguments.forecast_run_id or latest_forecast,
            inventory_risk_run_id=arguments.inventory_risk_run_id or latest_risk,
        )
    except EvaluationSetError as error:
        parser.error(str(error))
    finally:
        engine.dispose()

    output_root = arguments.project_root / "data" / "processed" / "agent_evaluations"
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"agent_evaluation_{result['evaluation_id']}.json"
    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Agent evaluation: {result['passed_count']}/{result['case_count']} passed; report: {output_path}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
