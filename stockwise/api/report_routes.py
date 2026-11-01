"""Read-only, bounded access to completed daily and weekly report artifacts."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from stockwise.api.schemas import (
    ReportArtifactDetailResponse,
    ReportArtifactListResponse,
    ReportArtifactSummaryResponse,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_TYPES = {"daily_inventory", "weekly_data_science"}
_SAFE_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_MAX_REPORT_BYTES = 5 * 1024 * 1024
_PRIVATE_FIELD_MARKERS = ("path", "file", "secret", "password", "token", "credential")


def _runs_directory(report_type: str) -> Path:
    if report_type not in REPORT_TYPES:
        raise HTTPException(status_code=404, detail="Report not found")
    project_root = PROJECT_ROOT.resolve()
    root = (project_root / "data" / "processed" / "reports" / report_type / "runs").resolve()
    try:
        root.relative_to(project_root)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Report not found") from error
    return root


def _report_paths(report_type: str, report_id: str) -> tuple[Path, Path]:
    if not _SAFE_REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    root = _runs_directory(report_type)
    run_directory = (root / report_id).resolve()
    try:
        run_directory.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Report not found") from error
    paths = (run_directory / "report.json", run_directory / "report.html")
    for path in paths:
        try:
            path.resolve().relative_to(run_directory)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Report not found") from error
    return paths


def _read_report(report_type: str, report_id: str) -> tuple[dict[str, Any], Path, Path]:
    json_path, html_path = _report_paths(report_type, report_id)
    if not json_path.is_file() or not html_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        if json_path.stat().st_size > _MAX_REPORT_BYTES:
            raise ValueError("report exceeds maximum size")
        value = json.loads(json_path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("report_type") != report_type
            or value.get("report_id") != report_id
            or not isinstance(value.get("summary"), dict)
            or not isinstance(value.get("data_provenance"), dict)
            or not isinstance(value.get("limitations"), str)
        ):
            raise ValueError("report schema mismatch")
        generated_at = datetime.fromisoformat(str(value["generated_at_utc"]))
        if generated_at.tzinfo is None:
            raise ValueError("report timestamp must include timezone")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as error:
        raise HTTPException(status_code=503, detail="Saved report artifact is invalid") from error
    return value, json_path, html_path


def _agent_summary(value: Any) -> dict[str, Any] | None:
    """Expose only already-validated findings and safe metadata, never source file paths."""
    if not isinstance(value, dict):
        return None
    findings = []
    for item in value.get("findings", []):
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                key: item[key]
                for key in (
                    "agent",
                    "status",
                    "summary",
                    "findings",
                    "limitations",
                    "verified_numerical_findings",
                    "decision_evidence_refs",
                    "evidence_refs",
                )
                if key in item
            }
        )
    metadata = value.get("metadata")
    safe_metadata = None
    if isinstance(metadata, dict):
        safe_metadata = {
            key: metadata[key]
            for key in (
                "llm_used",
                "explanation_source",
                "provider",
                "model_name",
                "schema_valid",
                "numeric_claims_valid",
                "evidence_references_valid",
                "agent_output_valid",
            )
            if key in metadata
        }
    return {
        key: value[key]
        for key in ("status", "selected_agents")
        if key in value
    } | {"findings": findings, "metadata": safe_metadata}


def _safe_provenance(value: dict[str, Any]) -> dict[str, Any]:
    """Keep compact provenance facts but omit path-like and credential-like fields."""
    return {
        key: item
        for key, item in value.items()
        if not any(marker in key.casefold() for marker in _PRIVATE_FIELD_MARKERS)
        and isinstance(item, (str, int, float, bool, type(None)))
        and len(str(item)) <= 240
    }


def _summary_response(
    report_type: str,
    report_id: str,
    value: dict[str, Any],
    html_available: bool,
) -> ReportArtifactSummaryResponse:
    return ReportArtifactSummaryResponse(
        report_id=report_id,
        report_type=report_type,
        status="completed",
        generated_at_utc=datetime.fromisoformat(str(value["generated_at_utc"])),
        source_run_id=value.get("source_run_id"),
        summary=value["summary"],
        data_provenance=_safe_provenance(value["data_provenance"]),
        limitations=value["limitations"],
        html_available=html_available,
    )


def _detail_response(report_type: str, report_id: str) -> ReportArtifactDetailResponse:
    value, _json_path, html_path = _read_report(report_type, report_id)
    summary = _summary_response(report_type, report_id, value, html_path.is_file())
    safe_fields = {
        key: value[key]
        for key in (
            "selection",
            "evaluation",
            "interpretation",
            "metric_explanations",
            "action_plan",
        )
        if key in value
    }
    safe_agent = _agent_summary(value.get("agent_intelligence"))
    return ReportArtifactDetailResponse(
        **summary.model_dump(),
        **safe_fields,
        agent_intelligence=safe_agent,
    )


@router.get("", response_model=ReportArtifactListResponse)
def list_reports(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    report_type: Literal["daily_inventory", "weekly_data_science"] | None = None,
) -> ReportArtifactListResponse:
    """List complete JSON+HTML report pairs, newest first, with bounded pagination."""
    report_types = [report_type] if report_type else sorted(REPORT_TYPES)
    candidates: list[tuple[float, str, str, Path]] = []
    for kind in report_types:
        root = _runs_directory(kind)
        if not root.is_dir():
            continue
        for json_path in root.glob("*/report.json"):
            report_id = json_path.parent.name
            if not _SAFE_REPORT_ID.fullmatch(report_id) or not json_path.with_name("report.html").is_file():
                continue
            try:
                modified = json_path.stat().st_mtime
            except OSError:
                continue
            candidates.append((modified, kind, report_id, json_path))
    candidates.sort(key=lambda item: (item[0], item[2]), reverse=True)
    page = candidates[offset : offset + limit]
    items = []
    for _modified, kind, report_id, _path in page:
        value, _json_path, html_path = _read_report(kind, report_id)
        items.append(_summary_response(kind, report_id, value, html_path.is_file()))
    return ReportArtifactListResponse(items=items, total=len(candidates), limit=limit, offset=offset)


@router.get("/{report_type}/{report_id}", response_model=ReportArtifactDetailResponse)
def get_report(
    report_type: Literal["daily_inventory", "weekly_data_science"],
    report_id: str,
) -> ReportArtifactDetailResponse:
    """Return a sanitized report view without internal source paths or artifact paths."""
    return _detail_response(report_type, report_id)


@router.get("/{report_type}/{report_id}/download")
def download_report(
    report_type: Literal["daily_inventory", "weekly_data_science"],
    report_id: str,
    format: Literal["json", "html"] = Query(default="json"),
) -> Response:
    """Download a sanitized JSON view or the generated human-readable HTML report."""
    _value, _json_path, html_path = _read_report(report_type, report_id)
    safe_detail = _detail_response(report_type, report_id)
    filename = f"{report_type}-{report_id}.{format}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if format == "html":
        try:
            content = html_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(status_code=503, detail="Saved HTML report is unavailable") from error
        return Response(content=content, media_type="text/html", headers=headers)
    return JSONResponse(
        content=safe_detail.model_dump(mode="json"),
        media_type="application/json",
        headers=headers,
    )
