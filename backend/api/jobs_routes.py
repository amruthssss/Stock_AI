"""Read-only polling API for PostgreSQL-backed background jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from backend.api.dependencies import get_database_engine
from backend.api.schemas import (
    BackgroundJobListResponse,
    BackgroundJobResponse,
    BackgroundJobSummaryResponse,
)
from backend.api.security import require_approval_admin
from backend.persistence.jobs import JobStateConflict, cancel_queued_job, get_job, list_jobs


router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=BackgroundJobListResponse)
def read_job_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_database_engine),
) -> BackgroundJobListResponse:
    """Return bounded safe metadata for the newest background jobs."""
    items, total = list_jobs(engine, limit=limit, offset=offset)
    return BackgroundJobListResponse(
        items=[BackgroundJobSummaryResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=BackgroundJobResponse)
def read_job_status(job_id: str, engine: Engine = Depends(get_database_engine)) -> BackgroundJobResponse:
    """Return bounded, secret-free status details for one unguessable job ID."""
    job = get_job(engine, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job was not found")
    return BackgroundJobResponse.model_validate(job)


@router.post("/{job_id}/cancel", response_model=BackgroundJobResponse, dependencies=[Depends(require_approval_admin)])
def cancel_job(job_id: str, engine: Engine = Depends(get_database_engine)) -> BackgroundJobResponse:
    """Cancel queued work; already-running jobs are not interrupted."""
    try:
        job = cancel_queued_job(engine, job_id)
    except JobStateConflict as error:
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled") from error
    if job is None:
        raise HTTPException(status_code=404, detail="Job was not found")
    return BackgroundJobResponse.model_validate(job)
