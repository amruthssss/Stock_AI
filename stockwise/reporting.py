"""Build deterministic daily inventory and weekly data-science report artifacts."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from stockwise.agent.checkpointing import enter_investigation_checkpointer
from stockwise.agent.investigation import run_investigation
from stockwise.api.config import Settings, get_settings
from stockwise.api.database import create_database_engine


class ReportInputError(ValueError):
    """A required report source is missing or does not match its expected schema."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportInputError(f"Could not read a valid JSON report source: {path.name}") from error
    if not isinstance(value, dict):
        raise ReportInputError(f"Report source must be a JSON object: {path.name}")
    return value


def _number(record: dict[str, Any], key: str, *, minimum: float | None = None) -> float:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReportInputError(f"Report source has an invalid numeric field: {key}")
    if minimum is not None and value < minimum:
        raise ReportInputError(f"Report source has an out-of-range numeric field: {key}")
    return float(value)


def _now(generated_at: datetime | None) -> datetime:
    value = generated_at or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return value.astimezone(UTC)


def _safe_source_path(base: Path, relative_name: str) -> Path:
    path = (base / relative_name).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as error:
        raise ReportInputError("Latest-run pointer references a path outside its data folder") from error
    return path


def _latest_inventory_source(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    base = project_root / "data" / "processed" / "inventory_risks"
    pointer = _read_json(base / "latest.json")
    report_name = pointer.get("risk_report")
    if not isinstance(report_name, str) or not report_name:
        raise ReportInputError("Inventory latest pointer is missing risk_report")
    report = _read_json(_safe_source_path(base, report_name))
    if report.get("status") != "completed" or not isinstance(report.get("results"), list):
        raise ReportInputError("Latest inventory-risk report is not completed or has no results")
    if not report["results"]:
        raise ReportInputError("Latest inventory-risk report contains no series rows")
    return pointer, report


def _latest_forecast_source(project_root: Path) -> dict[str, Any]:
    base = project_root / "data" / "processed" / "forecasts"
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for path in base.glob("baseline_*.json"):
        try:
            report = _read_json(path)
            if report.get("status") != "completed":
                continue
            generated_at = datetime.fromisoformat(str(report["generated_at_utc"]))
            if generated_at.tzinfo is None:
                continue
            candidates.append((generated_at, report))
        except (ReportInputError, KeyError, ValueError):
            continue
    if not candidates:
        raise ReportInputError("No completed forecast run is available under data/processed/forecasts")
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _inventory_summary(report: dict[str, Any]) -> dict[str, Any]:
    rows = report["results"]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("series_id"), str):
            raise ReportInputError("Inventory-risk results contain an invalid series row")
    highest_stockout = max(
        rows,
        key=lambda row: (_number(row, "stockout_probability_normal_approximation", minimum=0), row["series_id"]),
    )
    highest_expiry = max(
        rows,
        key=lambda row: (_number(row, "expiry_units_at_risk_fifo", minimum=0), row["series_id"]),
    )
    return {
        "series_count": len(rows),
        "series_with_order_recommendation": sum(
            _number(row, "suggested_order_quantity_units", minimum=0) > 0 for row in rows
        ),
        "recommended_order_units": sum(
            _number(row, "suggested_order_quantity_units", minimum=0) for row in rows
        ),
        "series_with_overstock": sum(_number(row, "overstock_units", minimum=0) > 0 for row in rows),
        "overstock_units": sum(_number(row, "overstock_units", minimum=0) for row in rows),
        "expiry_units_at_risk": sum(_number(row, "expiry_units_at_risk_fifo", minimum=0) for row in rows),
        "expiry_value_at_risk_proxy": sum(
            _number(row, "expiry_value_at_risk_proxy", minimum=0) for row in rows
        ),
        "highest_stockout": {
            "series_id": highest_stockout["series_id"],
            "probability": _number(
                highest_stockout, "stockout_probability_normal_approximation", minimum=0
            ),
        },
        "highest_expiry": {
            "series_id": highest_expiry["series_id"],
            "units": _number(highest_expiry, "expiry_units_at_risk_fifo", minimum=0),
        },
    }


def _inventory_intelligence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Create business-readable guidance from the already-validated risk fields."""
    explanations = [
        {
            "metric": "Stockout probability",
            "explanation": "An estimated probability that recorded demand could exceed available stock under the risk model.",
            "unit": "probability from 0 to 1",
            "calculation": "Provided by the inventory-risk run; this report does not recalculate it.",
            "limitation": "It is an estimate, not a certainty, and depends on synthetic inventory assumptions.",
        },
        {
            "metric": "Expiry units at risk",
            "explanation": "Units that the FIFO expiry calculation identifies as exposed to expiry.",
            "unit": "units",
            "calculation": "Provided by the inventory-risk run.",
            "limitation": "Batch dates and inventory values are synthetic unless the source run says otherwise.",
        },
        {
            "metric": "Expiry value at risk",
            "explanation": "An estimated value proxy for units exposed to expiry.",
            "unit": "source-report currency proxy",
            "calculation": "Provided by the inventory-risk run.",
            "limitation": "This is not confirmed cost, revenue, or financial loss.",
        },
        {
            "metric": "Recommended order quantity",
            "explanation": "A suggested replenishment quantity from the risk calculation.",
            "unit": "units",
            "calculation": "Provided by the inventory-risk run.",
            "limitation": "It is advisory and is not a purchase order; supplier terms and approvals still apply.",
        },
        {
            "metric": "Overstock units",
            "explanation": "Units above the source run's target or coverage calculation.",
            "unit": "units",
            "calculation": "Provided by the inventory-risk run.",
            "limitation": "Excess units are not automatically a loss; demand, holding cost, and expiry data are needed.",
        },
    ]
    actions: list[dict[str, Any]] = []
    for row in rows:
        series_id = row["series_id"]
        probability = _number(row, "stockout_probability_normal_approximation", minimum=0)
        order_units = _number(row, "suggested_order_quantity_units", minimum=0)
        overstock_units = _number(row, "overstock_units", minimum=0)
        expiry_units = _number(row, "expiry_units_at_risk_fifo", minimum=0)
        if probability > 0 and order_units > 0:
            actions.append(
                {
                    "series_id": series_id,
                    "issue": "Potential stockout with a replenishment suggestion",
                    "evidence": {
                        "stockout_probability": probability,
                        "recommended_order_units": order_units,
                    },
                    "recommended_action": "Review replenishment timing and supplier availability before approving an order.",
                    "alternative_action": "Check transfer or substitution options if replenishment lead time is too long.",
                    "required_human_decision": "A human must approve, change, or reject the advisory order.",
                    "missing_information": "Confirmed on-hand stock, incoming orders, supplier lead time, and service target.",
                }
            )
        if expiry_units > 0:
            actions.append(
                {
                    "series_id": series_id,
                    "issue": "Units exposed to expiry",
                    "evidence": {"expiry_units_at_risk": expiry_units},
                    "recommended_action": "Use FEFO rotation and review discount, bundle, or transfer options before expiry.",
                    "alternative_action": "Confirm supplier-return eligibility where applicable.",
                    "required_human_decision": "A human must choose an approved stock-movement or markdown action.",
                    "missing_information": "Verified batch expiry dates, location demand, and approved pricing rules.",
                }
            )
        if overstock_units > 0:
            actions.append(
                {
                    "series_id": series_id,
                    "issue": "Potential overstock",
                    "evidence": {"overstock_units": overstock_units},
                    "recommended_action": "Pause or reduce the next replenishment while reviewing demand and coverage.",
                    "alternative_action": "Consider an inter-store transfer or targeted promotion.",
                    "required_human_decision": "A human must confirm the target stock policy and any commercial action.",
                    "missing_information": "Holding cost, demand outlook, expiry dates, and transfer or return terms.",
                }
            )
    return {"metric_explanations": explanations, "action_plan": actions}


def _report_title(report: dict[str, Any]) -> str:
    return (
        "Daily Inventory Report"
        if report.get("report_type") == "daily_inventory"
        else "Weekly Data Science Report"
    )


def _display_text(value: Any, *, limit: int = 320) -> str:
    """Normalize untrusted report strings before placing them in human-readable output."""
    if isinstance(value, float) and math.isfinite(value):
        raw_text = f"{value:.4f}".rstrip("0").rstrip(".")
    else:
        raw_text = str(value)
    text = " ".join(raw_text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _summary_items(report: dict[str, Any]) -> list[tuple[str, str]]:
    """Map saved deterministic summary values to concise business-facing labels."""
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return []
    if report.get("report_type") == "daily_inventory":
        items = [
            ("Series reviewed", summary.get("series_count")),
            ("Series with suggested orders", summary.get("series_with_order_recommendation")),
            ("Suggested order units", summary.get("recommended_order_units")),
            ("Series with potential overstock", summary.get("series_with_overstock")),
            ("Units above the assumed target", summary.get("overstock_units")),
            ("Expiry units at risk", summary.get("expiry_units_at_risk")),
            ("Expiry value proxy, not confirmed loss", summary.get("expiry_value_at_risk_proxy")),
        ]
        stockout = summary.get("highest_stockout")
        if isinstance(stockout, dict):
            probability = stockout.get("probability")
            probability_text = (
                f"{probability:.1%} estimated probability"
                if isinstance(probability, (int, float)) and not isinstance(probability, bool)
                else _display_text(probability)
            )
            items.append(
                (
                    "Highest stockout signal",
                    f"{_display_text(stockout.get('series_id'))} · {probability_text}",
                )
            )
        expiry = summary.get("highest_expiry")
        if isinstance(expiry, dict):
            items.append(
                (
                    "Highest expiry exposure",
                    f"{_display_text(expiry.get('series_id'))} · {_display_text(expiry.get('units'))} units",
                )
            )
    else:
        items = [
            ("Best model by held-out MAE", summary.get("best_held_out_model_by_mae")),
            ("Held-out MAE · lower is closer", summary.get("best_held_out_mae")),
            ("Models compared", summary.get("candidate_count")),
            ("Held-out observations per model", summary.get("held_out_observations_per_model")),
        ]
    return [
        (label, _display_text(value))
        for label, value in items
        if value is not None
    ]


def _report_provenance_lines(report: dict[str, Any]) -> list[str]:
    provenance = report.get("data_provenance")
    lines: list[str] = []
    if isinstance(provenance, dict):
        for key, value in provenance.items():
            private_key = any(
                marker in key.casefold()
                for marker in ("path", "file", "secret", "password", "token", "credential")
            )
            if not private_key and isinstance(value, (str, int, float, bool)):
                lines.append(f"{key.replace('_', ' ').title()}: {_display_text(value)}")
    selection = report.get("selection")
    if isinstance(selection, dict):
        scope = [
            f"{key.replace('_', ' ')} {_display_text(selection[key])}"
            for key in ("state_id", "category_id", "series_count", "series_limit")
            if selection.get(key) is not None
        ]
        if scope:
            lines.append("Evaluation scope: " + ", ".join(scope))
    return lines


def _report_detail_lines(report: dict[str, Any]) -> list[str]:
    """Build a short readable detail section without serializing report JSON."""
    if report.get("report_type") == "daily_inventory":
        actions = report.get("action_plan")
        if not isinstance(actions, list):
            return []
        lines = []
        for item in actions[:5]:
            if not isinstance(item, dict):
                continue
            series_id = _display_text(item.get("series_id", "Series"), limit=120)
            issue = _display_text(item.get("issue", "Review item"), limit=160)
            action = _display_text(item.get("recommended_action", "Review the source report."))
            lines.append(f"{series_id} — {issue}. Suggested review: {action}")
        if len(actions) > 5:
            lines.append(f"The saved report contains {len(actions) - 5} additional review items.")
        return lines

    evaluation = report.get("evaluation")
    held_out = evaluation.get("held_out") if isinstance(evaluation, dict) else None
    if not isinstance(held_out, list):
        return []
    lines = ["Held-out model comparison (lower MAE and WAPE indicate closer recorded-sales forecasts):"]
    for row in held_out[:5]:
        if isinstance(row, dict):
            lines.append(
                f"{_display_text(row.get('model_name'))}: "
                f"MAE {_display_text(row.get('mae'))}; "
                f"WAPE {_display_text(row.get('wape'))}; "
                f"bias {_display_text(row.get('bias'))}; "
                f"{_display_text(row.get('observations'))} observations"
            )
    return lines


def _metric_explanation_lines(report: dict[str, Any]) -> list[str]:
    """Return saved, plain-language metric explanations for business readers."""
    interpretation = report.get("interpretation")
    if not isinstance(interpretation, dict):
        return []
    return [
        _display_text(interpretation[key], limit=500)
        for key in ("plain_language", "recommended_use", "missing_information")
        if isinstance(interpretation.get(key), str) and interpretation[key].strip()
    ]


def render_report_email_text(report: dict[str, Any]) -> str:
    """Render a readable plain-text report email without exposing raw JSON or paths."""
    title = _report_title(report)
    generated = _display_text(report.get("generated_at_utc", "Not recorded"))
    lines = [
        f"StockWise | {title}",
        f"Generated: {generated}",
        "",
        "Executive summary",
        *[f"- {label}: {value}" for label, value in _summary_items(report)],
    ]
    source_run_id = report.get("source_run_id")
    if source_run_id:
        lines.extend(["", f"Source run: {_display_text(source_run_id, limit=120)}"])
    details = _report_detail_lines(report)
    if details:
        lines.extend(["", "Important findings and review items", *[f"- {line}" for line in details]])
    metric_explanations = _metric_explanation_lines(report)
    if metric_explanations:
        lines.extend(["", "How to interpret these metrics", *[f"- {line}" for line in metric_explanations]])
    provenance = _report_provenance_lines(report)
    if provenance:
        lines.extend(["", "Data source and scope", *[f"- {line}" for line in provenance]])
    limitations = report.get("limitations")
    if limitations:
        lines.extend(["", "Assumptions and limitations", _display_text(limitations, limit=1200)])
    lines.extend(
        [
            "",
            "This report is decision support. Recommendations require human review; no purchase or price change was made.",
            f"Report reference: {_display_text(report.get('report_id', 'not recorded'), limit=120)}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_report_email_html(report: dict[str, Any]) -> str:
    """Render a compact, email-client-friendly HTML summary from validated report fields."""
    title = html.escape(_report_title(report))
    generated = html.escape(_display_text(report.get("generated_at_utc", "Not recorded")))
    cards = "".join(
        "<tr>"
        f"<td style='padding:12px;border:1px solid #dce7df;background:#f5f8f4;vertical-align:top'>"
        f"<div style='font-size:12px;color:#607268'>{html.escape(label)}</div>"
        f"<strong style='display:block;margin-top:5px;color:#173c2e'>{html.escape(value)}</strong></td>"
        "</tr>"
        for label, value in _summary_items(report)
    )
    details = _report_detail_lines(report)
    detail_html = "".join(f"<li style='margin:0 0 9px'>{html.escape(line)}</li>" for line in details)
    metric_explanations = _metric_explanation_lines(report)
    metric_html = "".join(
        f"<li style='margin:0 0 9px'>{html.escape(line)}</li>"
        for line in metric_explanations
    )
    provenance = _report_provenance_lines(report)
    provenance_html = "".join(f"<li>{html.escape(line)}</li>" for line in provenance)
    limitations = html.escape(_display_text(report.get("limitations", "See the report reference."), limit=1200))
    source_run_id = html.escape(_display_text(report.get("source_run_id", "Not recorded"), limit=120))
    report_id = html.escape(_display_text(report.get("report_id", "Not recorded"), limit=120))
    findings_section = (
        f"<h2 style='font-size:16px;color:#173c2e'>Important findings and review items</h2><ul>{detail_html}</ul>"
        if detail_html
        else ""
    )
    metric_section = (
        f"<h2 style='font-size:16px;color:#173c2e'>How to interpret these metrics</h2><ul>{metric_html}</ul>"
        if metric_html
        else ""
    )
    provenance_section = (
        f"<h2 style='font-size:16px;color:#173c2e'>Data source and scope</h2><ul>{provenance_html}</ul>"
        if provenance_html
        else ""
    )
    return (
        "<!doctype html><html lang='en'><body style='margin:0;background:#f2f6f2;font-family:Arial,sans-serif;color:#263a30'>"
        "<table role='presentation' style='width:100%;border-collapse:collapse'><tr><td align='center' style='padding:22px 10px'>"
        "<table role='presentation' style='width:100%;max-width:680px;border-collapse:collapse;background:#fff;border:1px solid #dce7df'>"
        "<tr><td style='padding:22px 24px;background:#123a2b;color:#f5ffe9'>"
        "<div style='font-size:11px;font-weight:bold;letter-spacing:2px'>STOCKWISE · DECISION SUPPORT</div>"
        f"<h1 style='margin:9px 0 4px;font-size:25px'>{title}</h1>"
        f"<div style='font-size:13px;color:#d6e8d9'>Generated {generated}</div></td></tr>"
        "<tr><td style='padding:18px 24px'><h2 style='margin:0 0 12px;font-size:16px;color:#173c2e'>Executive summary</h2>"
        f"<table role='presentation' style='width:100%;border-collapse:collapse'>{cards}</table>"
        f"{findings_section}{metric_section}{provenance_section}"
        f"<h2 style='font-size:16px;color:#173c2e'>Assumptions and limitations</h2><p style='font-size:13px;line-height:1.6'>{limitations}</p>"
        "<p style='padding:12px;background:#f5f8f4;font-size:12px;line-height:1.6'>This report is decision support. Recommendations require human review; no purchase or price change was made.</p>"
        f"<p style='font-size:11px;color:#68776e'>Source run: {source_run_id}<br>Report reference: {report_id}</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )


def _run_report_intelligence(
    *,
    report_type: str,
    source_run_id: str | None,
    engine: Engine | None,
    checkpointer: BaseCheckpointSaver | None,
    settings: Settings | None,
) -> dict[str, Any]:
    """Run the existing evidence-backed graph when its persistence services are available."""
    if engine is None or checkpointer is None:
        return {
            "status": "not_run",
            "reason": "report_generation_services_not_configured",
            "selected_agents": [],
            "findings": [],
            "evidence": [],
        }

    domains = ["inventory-risk", "forecast", "decision"]
    question = (
        "Explain the saved inventory risks, evaluate recovery options, and prioritize "
        "human-approved actions for this daily report."
        if report_type == "daily_inventory"
        else "Explain the saved forecast evidence, inventory implications, and decision trade-offs "
        "for this weekly report."
    )
    run_ids = {}
    if source_run_id and report_type == "daily_inventory":
        run_ids["inventory-risk"] = source_run_id
    try:
        result = run_investigation(
            engine,
            checkpointer=checkpointer,
            question=question,
            domains=domains,
            run_ids=run_ids,
            include_business_intelligence=True,
            settings=settings,
        )
    except (SQLAlchemyError, OSError, RuntimeError, ValueError) as error:
        return {
            "status": "fallback",
            "reason": f"agent_execution_failed:{type(error).__name__}",
            "selected_agents": [],
            "findings": [],
            "evidence": [],
        }

    results = result.get("agent_results", [])
    findings = [
        {
            "agent": item.get("agent"),
            "status": item.get("status"),
            "summary": item.get("summary"),
            "findings": item.get("findings", []),
            "limitations": item.get("limitations", []),
            "failure_reason": item.get("failure_reason"),
            "verified_numerical_findings": item.get("verified_numerical_findings", []),
            "decision_evidence_refs": item.get("decision_evidence_refs", []),
            "evidence_refs": [
                reference
                for finding in item.get("findings", [])
                if isinstance(finding, dict)
                for reference in finding.get("evidence_refs", [])
            ],
        }
        for item in results
        if isinstance(item, dict)
    ]
    evidence = [
        {
            "evidence_id": item.get("evidence_id"),
            "source_run_id": item.get("source_run_id"),
            "source_table": item.get("source_table"),
            "source_path": item.get("source_path"),
            "domain": item.get("domain"),
        }
        for item in result.get("evidence", [])
        if isinstance(item, dict)
    ]
    return {
        "status": result.get("status", "completed"),
        "answer": result.get("answer"),
        "selected_agents": result.get("selected_agents", []),
        "findings": findings,
        "evidence": evidence,
        "metadata": {
            key: result.get(key)
            for key in (
                "source_run_ids",
                "llm_used",
                "explanation_source",
                "provider",
                "model_name",
                "llm_fallback_reason",
                "schema_valid",
                "numeric_claims_valid",
                "evidence_references_valid",
                "agent_output_valid",
            )
        },
    }


def _forecast_metrics(report: dict[str, Any], period: str) -> list[dict[str, Any]]:
    evaluation = report.get("evaluation")
    period_data = evaluation.get(period) if isinstance(evaluation, dict) else None
    model_data = period_data.get("models") if isinstance(period_data, dict) else None
    if not isinstance(model_data, dict) or not model_data:
        raise ReportInputError(f"Forecast run is missing {period} model metrics")
    result: list[dict[str, Any]] = []
    for model_name, metrics in model_data.items():
        if not isinstance(metrics, dict):
            raise ReportInputError(f"Forecast metric row for {model_name} is invalid")
        result.append(
            {
                "model_name": model_name,
                "mae": _number(metrics, "mae", minimum=0),
                "wape": None if metrics.get("wape") is None else _number(metrics, "wape", minimum=0),
                "bias": None if metrics.get("bias") is None else _number(metrics, "bias"),
                "observations": int(_number(metrics, "observations", minimum=1)),
            }
        )
    return sorted(result, key=lambda row: (row["mae"], row["model_name"]))


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _render_html(title: str, report: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    safe_title = html.escape(title)
    generated = html.escape(str(report["generated_at_utc"]))
    summary_cards = "".join(
        "<div class='summary-card'>"
        f"<span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in _summary_items(report)
    )
    table_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row.values()) + "</tr>"
        for row in rows
    )
    headers = "".join(f"<th>{html.escape(name.replace('_', ' ').title())}</th>" for name in rows[0])
    agent_intelligence = report.get("agent_intelligence", {})
    agent_items = agent_intelligence.get("findings", []) if isinstance(agent_intelligence, dict) else []
    agent_rows = "".join(
        "<li><strong>"
        + html.escape(str(item.get("agent", "agent")).replace("_", " ").title())
        + " ("
        + html.escape(str(item.get("status", "unknown")))
        + "):</strong> "
        + html.escape(str(item.get("summary", "")))
        + "</li>"
        for item in agent_items
        if isinstance(item, dict)
    )
    intelligence_section = (
        f"<h2>Validated agent interpretation</h2><ul>{agent_rows}</ul>"
        if agent_rows
        else "<h2>Validated agent interpretation</h2><p>No agent interpretation was executed for this artifact.</p>"
    )
    report_details = "".join(
        f"<li>{html.escape(line)}</li>" for line in _report_detail_lines(report)
    )
    details_section = (
        f"<h2>Important findings and review items</h2><ul>{report_details}</ul>"
        if report_details
        else ""
    )
    metric_explanations = "".join(
        f"<li>{html.escape(line)}</li>" for line in _metric_explanation_lines(report)
    )
    metric_section = (
        f"<h2>How to interpret these metrics</h2><ul>{metric_explanations}</ul>"
        if metric_explanations
        else ""
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{safe_title}</title><style>"
        "body{font-family:Arial,sans-serif;line-height:1.45;color:#1f2937;max-width:1100px;margin:2rem auto;padding:0 1rem}"
        "h1{color:#123b5d}h2{border-bottom:2px solid #dbeafe;padding-bottom:.3rem}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #d1d5db;padding:.5rem;text-align:left}"
        "th{background:#e5eef7}.notice{background:#fff7ed;border-left:4px solid #f97316;padding:.8rem}"
        "li{margin:.5rem 0}.evidence{font-family:monospace;font-size:.9em}"
        ".summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.65rem}"
        ".summary-card{border:1px solid #dce7df;border-radius:10px;background:#f5f8f4;padding:.8rem}"
        ".summary-card span{display:block;color:#607268;font-size:.8rem}.summary-card strong{display:block;margin-top:.4rem;color:#173c2e}"
        "</style></head><body>"
        f"<h1>{safe_title}</h1><p><strong>Generated:</strong> {generated}</p>"
        f"<h2>Executive summary</h2><div class='summary-grid'>{summary_cards}</div>"
        f"<p><strong>Source run:</strong> {html.escape(_display_text(report.get('source_run_id', 'Not recorded'), limit=120))}</p>"
        f"<h2>Data source and scope</h2><ul>{''.join(f'<li>{html.escape(line)}</li>' for line in _report_provenance_lines(report))}</ul>"
        f"{details_section}{metric_section}"
        f"<p class=\"notice\"><strong>Important:</strong> {html.escape(report['limitations'])}</p>"
        f"{intelligence_section}"
        "<h2>Detailed data</h2><table><thead><tr>"
        f"{headers}</tr></thead><tbody>{table_rows}</tbody></table>"
        "</body></html>"
    )


def _publish_report(
    project_root: Path,
    *,
    report_type: str,
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    generated_at: datetime,
) -> Path:
    base = project_root / "data" / "processed" / "reports" / report_type
    run_id = generated_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_directory = base / "runs" / run_id
    report["report_id"] = run_id
    report["artifacts"] = {"json": "report.json", "html": "report.html"}
    _write_atomic(run_directory / "report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    title = "Daily Inventory Report" if report_type == "daily_inventory" else "Weekly Data Science Report"
    _write_atomic(run_directory / "report.html", _render_html(title, report, rows))
    pointer = {"report_id": run_id, "report_file": f"runs/{run_id}/report.json"}
    _write_atomic(base / "latest.json", json.dumps(pointer, indent=2) + "\n")
    return run_directory / "report.json"


def build_daily_inventory_report(
    project_root: Path,
    *,
    generated_at: datetime | None = None,
    engine: Engine | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    settings: Settings | None = None,
) -> Path:
    """Summarize the latest risk run; inventory inputs remain explicitly synthetic."""
    project_root = Path(project_root).resolve()
    timestamp = _now(generated_at)
    pointer, source = _latest_inventory_source(project_root)
    rows = source["results"]
    summary = _inventory_summary(source)
    report = {
        "schema_version": 1,
        "report_type": "daily_inventory",
        "generated_at_utc": timestamp.isoformat(),
        "source_run_id": source.get("run_id"),
        "source_report": pointer["risk_report"],
        "data_provenance": source.get("data_provenance", {}),
        "summary": summary,
        **_inventory_intelligence(rows),
        "agent_intelligence": _run_report_intelligence(
            report_type="daily_inventory",
            source_run_id=source.get("run_id"),
            engine=engine,
            checkpointer=checkpointer,
            settings=settings,
        ),
        "limitations": "Operational inventory values are synthetic assumptions; this report is decision support, not a purchase order.",
    }
    table = [
        {
            "series_id": row["series_id"],
            "stockout_probability": _number(
                row, "stockout_probability_normal_approximation", minimum=0
            ),
            "order_quantity_units": int(_number(row, "suggested_order_quantity_units", minimum=0)),
            "overstock_units": int(_number(row, "overstock_units", minimum=0)),
            "expiry_units": _number(row, "expiry_units_at_risk_fifo", minimum=0),
        }
        for row in rows
    ]
    return _publish_report(
        project_root,
        report_type="daily_inventory",
        report=report,
        rows=table,
        generated_at=timestamp,
    )


def build_weekly_data_science_report(
    project_root: Path,
    *,
    generated_at: datetime | None = None,
    engine: Engine | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    settings: Settings | None = None,
) -> Path:
    """Compare the latest saved forecast candidates on the held-out M5 period."""
    project_root = Path(project_root).resolve()
    timestamp = _now(generated_at)
    source = _latest_forecast_source(project_root)
    held_out = _forecast_metrics(source, "held_out")
    rolling = _forecast_metrics(source, "rolling_origin")
    selection = source.get("selection", {})
    provenance = source.get("data_provenance", {})
    if not isinstance(selection, dict) or not isinstance(provenance, dict):
        raise ReportInputError("Forecast report is missing selection or provenance")
    report = {
        "schema_version": 1,
        "report_type": "weekly_data_science",
        "generated_at_utc": timestamp.isoformat(),
        "source_run_id": source.get("run_id"),
        "source_generated_at_utc": source.get("generated_at_utc"),
        "data_provenance": provenance,
        "selection": selection,
        "evaluation": {"held_out": held_out, "rolling_origin": rolling},
        "summary": {
            "best_held_out_model_by_mae": held_out[0]["model_name"],
            "best_held_out_mae": held_out[0]["mae"],
            "candidate_count": len(held_out),
            "held_out_observations_per_model": held_out[0]["observations"],
        },
        "interpretation": {
            "plain_language": "Lower MAE and WAPE mean the model's recorded-sales forecasts were closer to observed sales in this evaluation.",
            "recommended_use": "Use the best comparable model as an input to review, not as an automatic purchasing decision.",
            "missing_information": "The report does not establish true demand where stockouts may have censored recorded sales.",
        },
        "agent_intelligence": _run_report_intelligence(
            report_type="weekly_data_science",
            source_run_id=source.get("run_id"),
            engine=engine,
            checkpointer=checkpointer,
            settings=settings,
        ),
        "limitations": "M5 metrics are observed-sales evaluation results for the recorded subset; they do not establish retail savings or full-category performance.",
    }
    table = [
        {
            "model_name": metric["model_name"],
            "held_out_mae": metric["mae"],
            "held_out_wape": metric["wape"],
            "held_out_bias": metric["bias"],
            "observations": metric["observations"],
        }
        for metric in held_out
    ]
    return _publish_report(
        project_root,
        report_type="weekly_data_science",
        report=report,
        rows=table,
        generated_at=timestamp,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a StockWise scheduled report artifact.")
    parser.add_argument("--kind", choices=("daily-inventory", "weekly-data-science"), required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    builder = (
        build_daily_inventory_report
        if arguments.kind == "daily-inventory"
        else build_weekly_data_science_report
    )
    settings = get_settings(arguments.project_root)
    engine = None
    try:
        if settings.database_url:
            engine = create_database_engine(settings.database_url)
        with ExitStack() as stack:
            checkpointer = (
                enter_investigation_checkpointer(engine, stack)
                if engine is not None
                else None
            )
            report_path = builder(
                arguments.project_root,
                engine=engine,
                checkpointer=checkpointer,
                settings=settings,
            )
    except ReportInputError as error:
        parser.error(str(error))
    except (SQLAlchemyError, OSError, ValueError) as error:
        parser.error(f"Could not initialize report services: {type(error).__name__}")
    finally:
        if engine is not None:
            engine.dispose()
    print(f"Report generated: {report_path}")


if __name__ == "__main__":
    main()
