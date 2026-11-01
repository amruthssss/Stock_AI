"""Read-only StockWise API routes for n8n workflow and execution metadata."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi import Path as ApiPath

from backend.api.n8n_client import N8nApiClient, N8nIntegrationError

router = APIRouter(prefix="/api/v1/n8n", tags=["n8n"])
_WORKFLOW_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


def _client(request: Request) -> N8nApiClient:
    settings = request.app.state.settings
    try:
        return N8nApiClient(
            base_url=settings.n8n_base_url,
            api_key_file=settings.n8n_api_key_file,
            timeout_seconds=settings.n8n_timeout_seconds,
        )
    except ValueError:
        raise N8nIntegrationError(
            "invalid_configuration", "The n8n API URL configuration is invalid.", 503
        ) from None


@router.get("/workflows")
def list_workflows(
    request: Request,
    limit: int = Query(default=100, ge=1, le=250),
    cursor: str | None = Query(default=None, max_length=500),
) -> dict:
    """List all returned workflows without filtering inactive or archived entries."""
    return _client(request).list_workflows(limit=limit, cursor=cursor)


@router.get("/workflows/{workflow_id}")
def get_workflow(
    request: Request,
    workflow_id: str = ApiPath(pattern=_WORKFLOW_ID_PATTERN),
) -> dict:
    """Read workflow state and node identities, excluding config and credentials."""
    return _client(request).get_workflow(workflow_id)


@router.get("/executions")
def list_executions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=250),
    cursor: str | None = Query(default=None, max_length=500),
    workflow_id: str | None = Query(
        default=None, alias="workflowId", max_length=128, pattern=_WORKFLOW_ID_PATTERN
    ),
) -> dict:
    """List execution status/timestamps; upstream execution data is never requested."""
    return _client(request).list_executions(
        limit=limit, cursor=cursor, workflow_id=workflow_id
    )


@router.get("/executions/{execution_id}")
def get_execution(
    request: Request,
    execution_id: str = ApiPath(pattern=r"^[1-9][0-9]{0,19}$"),
) -> dict:
    """Read one execution's summary without requesting node inputs or outputs."""
    return _client(request).get_execution(execution_id)
