"""Read-only, secret-safe access to the local n8n Public API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class N8nIntegrationError(Exception):
    """A safe-to-display n8n integration error with no upstream body text."""

    def __init__(self, category: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.status_code = status_code


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the API-key header can never be forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class N8nApiClient:
    """GET-only wrapper for documented n8n workflow and execution endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key_file: Path | None,
        timeout_seconds: float = 8.0,
        opener: Any | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.api_key_file = api_key_file
        self.timeout_seconds = timeout_seconds if 1 <= timeout_seconds <= 30 else 8.0
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    def list_workflows(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        """Return one cursor page; no active/archive filters are applied."""
        return self._get_page("workflows", limit=limit, cursor=cursor, extra={"excludePinnedData": "true"})

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Return safe workflow metadata; node parameters and credentials are omitted."""
        _validate_id(workflow_id)
        raw = self._get_json(f"workflows/{urllib.parse.quote(workflow_id, safe='')}", {"excludePinnedData": "true"})
        return _workflow_summary(raw, include_nodes=True)

    def list_executions(
        self, *, limit: int, cursor: str | None, workflow_id: str | None
    ) -> dict[str, Any]:
        """Return execution metadata only, explicitly excluding run payload data."""
        extra = {"includeData": "false", "redactExecutionData": "true"}
        if workflow_id:
            _validate_id(workflow_id)
            extra["workflowId"] = workflow_id
        page = self._get_page("executions", limit=limit, cursor=cursor, extra=extra)
        for execution in page["items"]:
            if execution["status"] in {"error", "crashed"} and execution["id"]:
                self._add_redacted_failure_summary(execution)
        return page

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        """Return safe execution metadata without node inputs, outputs, or workflow data."""
        if not execution_id.isdecimal() or int(execution_id) < 1:
            raise ValueError("Execution ID must be a positive integer.")
        raw = self._get_json(
            f"executions/{urllib.parse.quote(execution_id, safe='')}",
            {"includeData": "false", "redactExecutionData": "true"},
        )
        summary = _execution_summary(raw)
        if summary["status"] in {"error", "crashed"}:
            self._add_redacted_failure_summary(summary)
        return summary

    def _add_redacted_failure_summary(self, execution: dict[str, Any]) -> None:
        """Read only n8n's redacted failed-node metadata, never expose run data."""
        execution_id = execution.get("id")
        if not isinstance(execution_id, str) or not execution_id.isdecimal():
            execution["failure_summary"] = "Execution failed; redacted diagnostics are unavailable."
            return
        try:
            raw = self._get_json(
                f"executions/{urllib.parse.quote(execution_id, safe='')}",
                {"includeData": "true", "redactExecutionData": "true"},
            )
        except N8nIntegrationError:
            execution["failure_summary"] = "Execution failed; redacted diagnostics are unavailable."
            return
        data = raw.get("data")
        result = data.get("resultData") if isinstance(data, dict) else None
        last_node = result.get("lastNodeExecuted") if isinstance(result, dict) else None
        if isinstance(last_node, str) and last_node:
            execution["failed_node"] = last_node[:120]
            execution["failure_summary"] = f"Failed while running {last_node[:120]}. Payload is redacted."
        else:
            execution["failure_summary"] = "Execution failed; the failed node is unavailable. Payload is redacted."

    def _get_page(
        self,
        endpoint: str,
        *,
        limit: int,
        cursor: str | None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        parameters = {"limit": str(limit), **(extra or {})}
        if cursor:
            if len(cursor) > 500:
                raise ValueError("Pagination cursor is too long.")
            parameters["cursor"] = cursor
        raw = self._get_json(endpoint, parameters)
        rows = raw.get("data")
        if not isinstance(rows, list):
            raise N8nIntegrationError("invalid_response", "n8n returned an invalid list response.", 502)
        if endpoint == "workflows":
            items = [_workflow_summary(row, include_nodes=True) for row in rows if isinstance(row, dict)]
        else:
            items = [_execution_summary(row) for row in rows if isinstance(row, dict)]
        next_cursor = raw.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise N8nIntegrationError("invalid_response", "n8n returned an invalid page cursor.", 502)
        return {"items": items, "next_cursor": next_cursor}

    def _get_json(self, endpoint: str, query: dict[str, str]) -> dict[str, Any]:
        api_key = _read_api_key(self.api_key_file)
        url = f"{self.base_url}/{endpoint}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-N8N-API-KEY": api_key},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise N8nIntegrationError(
                    "unauthorized", "n8n rejected the configured API key.", 502
                ) from None
            if error.code == 404:
                raise N8nIntegrationError("not_found", "The requested n8n record was not found.", 404) from None
            if error.code == 429 or error.code >= 500:
                raise N8nIntegrationError(
                    "unavailable", "The n8n API is temporarily unavailable.", 503
                ) from None
            raise N8nIntegrationError("upstream_error", "The n8n API rejected the request.", 502) from None
        except TimeoutError:
            raise N8nIntegrationError("timeout", "The n8n API request timed out.", 504) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise N8nIntegrationError("timeout", "The n8n API request timed out.", 504) from None
            raise N8nIntegrationError(
                "unavailable", "The n8n API could not be reached.", 503
            ) from None
        except OSError:
            raise N8nIntegrationError(
                "unavailable", "The n8n API could not be reached.", 503
            ) from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise N8nIntegrationError("invalid_response", "n8n returned an oversized response.", 502)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise N8nIntegrationError("invalid_response", "n8n returned invalid JSON.", 502) from None
        if not isinstance(value, dict):
            raise N8nIntegrationError("invalid_response", "n8n returned an invalid response.", 502)
        return value


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except (TypeError, ValueError):
        port = None
        if ":" in parsed.netloc:
            raise ValueError("Invalid n8n API port.") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/api/v1"
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("n8n base URL must be a local HTTP /api/v1 URL.")
    return value.rstrip("/")


def _read_api_key(secret_file: Path | None) -> str:
    if secret_file is None:
        raise N8nIntegrationError("not_configured", "The n8n API key file is not configured.", 503)
    try:
        value = secret_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        value = ""
    if not value:
        raise N8nIntegrationError("not_configured", "The n8n API key file is unavailable.", 503)
    if "\n" in value or "\r" in value:
        raise N8nIntegrationError("not_configured", "The n8n API key file is invalid.", 503)
    return value


def _workflow_summary(value: dict[str, Any], *, include_nodes: bool) -> dict[str, Any]:
    nodes = value.get("nodes")
    node_rows = nodes if isinstance(nodes, list) else []
    active_version = value.get("activeVersionId")
    active = active_version is not None if "activeVersionId" in value else bool(value.get("active"))
    result: dict[str, Any] = {
        "id": _safe_text(value.get("id")),
        "name": _safe_text(value.get("name"), "Unnamed workflow"),
        "active": active,
        "is_archived": bool(value.get("isArchived", False)),
        "created_at": _safe_text(value.get("createdAt")),
        "updated_at": _safe_text(value.get("updatedAt")),
        "timezone": _safe_text((value.get("settings") or {}).get("timezone"))
        if isinstance(value.get("settings"), dict)
        else None,
        "node_count": len(node_rows),
        "trigger_count": int(value.get("triggerCount", 0)) if isinstance(value.get("triggerCount", 0), int) else 0,
    }
    if include_nodes:
        schedule, trigger_names = _trigger_summary(node_rows)
        result["nodes"] = [
            {
                "id": _safe_text(node.get("id")),
                "name": _safe_text(node.get("name"), "Unnamed node"),
                "type": _safe_text(node.get("type")),
                "disabled": bool(node.get("disabled", False)),
                "is_trigger": _safe_text(node.get("type")).lower().endswith("trigger"),
            }
            for node in node_rows
            if isinstance(node, dict)
        ]
        result["schedule"] = schedule
        result["trigger_names"] = trigger_names
    return result


def _execution_summary(value: dict[str, Any]) -> dict[str, Any]:
    """Allow-list execution metadata; never return n8n's data/workflowData fields."""
    started_at = _safe_text(value.get("startedAt"))
    stopped_at = _safe_text(value.get("stoppedAt"))
    duration_ms = _duration_ms(started_at, stopped_at)
    return {
        "id": _safe_text(value.get("id")),
        "workflow_id": _safe_text(value.get("workflowId")),
        "status": _safe_text(value.get("status"), "unknown"),
        "mode": _safe_text(value.get("mode")),
        "created_at": _safe_text(value.get("createdAt")),
        "started_at": started_at,
        "stopped_at": stopped_at,
        "duration_ms": duration_ms,
        "finished": bool(value.get("finished", False)),
        "failed_node": None,
        "failure_summary": None,
    }


def _duration_ms(started_at: str | None, stopped_at: str | None) -> int | None:
    if not started_at or not stopped_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        stop = datetime.fromisoformat(stopped_at)
    except ValueError:
        return None
    return max(0, int((stop - start).total_seconds() * 1000))


def _trigger_summary(nodes: list[Any]) -> tuple[str, list[str]]:
    trigger_nodes = [
        node
        for node in nodes
        if isinstance(node, dict)
        and isinstance(node.get("type"), str)
        and node["type"].lower().endswith("trigger")
    ]
    names = [
        _safe_text(node.get("name"), "Unnamed trigger") or "Unnamed trigger"
        for node in trigger_nodes
    ]
    schedules: list[str] = []
    for node in trigger_nodes:
        parameters = node.get("parameters")
        rule = parameters.get("rule") if isinstance(parameters, dict) else None
        intervals = rule.get("interval") if isinstance(rule, dict) else None
        if not isinstance(intervals, list):
            continue
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            hour = interval.get("triggerAtHour")
            if isinstance(interval.get("hoursInterval"), int):
                schedules.append(f"Every {interval['hoursInterval']} hours")
            elif isinstance(interval.get("weeks"), int) or isinstance(interval.get("triggerAtDay"), list):
                days = interval.get("triggerAtDay", [])
                day_names = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}
                day_text = ", ".join(day_names.get(day, "day") for day in days if isinstance(day, int))
                time_text = f" at {hour:02d}:00" if isinstance(hour, int) and 0 <= hour <= 23 else ""
                schedules.append(f"Weekly on {day_text or 'configured day'}{time_text}")
            elif isinstance(hour, int) and 0 <= hour <= 23:
                schedules.append(f"Daily at {hour:02d}:00")
    if schedules:
        return "; ".join(dict.fromkeys(schedules)), names
    if names:
        return "; ".join(names), names
    return "No trigger configured", names


def _safe_text(value: Any, default: str | None = None) -> str | None:
    if isinstance(value, str):
        return value[:500]
    return default


def _validate_id(value: str) -> None:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("Workflow ID contains invalid characters.")
