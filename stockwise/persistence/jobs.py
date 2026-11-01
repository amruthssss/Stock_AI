"""Transactional enqueueing, leasing, status updates, and stale-job recovery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockwise.persistence.models import BackgroundJob


DEFAULT_MAX_ATTEMPTS = 3
MAX_ALLOWED_ATTEMPTS = 5


class JobIdempotencyConflict(ValueError):
    """Raised when a key is reused for a different job request."""


class JobStateConflict(ValueError):
    """Raised when a worker attempts to update a job it does not own."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    worker_id: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def serialize_job(job: BackgroundJob) -> dict[str, Any]:
    """Return API-safe job fields without the idempotency key or worker ID."""
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "created_at_utc": job.created_at_utc,
        "updated_at_utc": job.updated_at_utc,
        "result": job.result,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def enqueue_job(
    engine: Engine,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[dict[str, Any], bool]:
    """Create a queued job or return the matching prior request."""
    if not job_type or len(job_type) > 64:
        raise ValueError("job_type must contain between 1 and 64 characters")
    if not 1 <= max_attempts <= MAX_ALLOWED_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ALLOWED_ATTEMPTS}")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("idempotency_key must contain between 1 and 128 characters")

    now = utc_now()
    job = BackgroundJob(
        job_id=uuid.uuid4().hex,
        job_type=job_type,
        status="queued",
        payload=payload,
        idempotency_key=idempotency_key,
        attempt_count=0,
        max_attempts=max_attempts,
        created_at_utc=now,
        updated_at_utc=now,
        available_at=now,
    )
    with Session(engine) as session:
        existing = session.scalar(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return _existing_or_conflict(existing, job_type, payload), False
        session.add(job)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise
            return _existing_or_conflict(existing, job_type, payload), False
        session.refresh(job)
        return serialize_job(job), True


def _existing_or_conflict(
    existing: BackgroundJob,
    job_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if existing.job_type != job_type or existing.payload != payload:
        raise JobIdempotencyConflict("idempotency key already belongs to a different job request")
    return serialize_job(existing)


def get_job(engine: Engine, job_id: str) -> dict[str, Any] | None:
    with Session(engine) as session:
        job = session.get(BackgroundJob, job_id)
        return serialize_job(job) if job is not None else None


def list_jobs(
    engine: Engine,
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return a bounded newest-first job history without request secrets."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    with Session(engine) as session:
        total = session.scalar(select(func.count()).select_from(BackgroundJob)) or 0
        jobs = session.scalars(
            select(BackgroundJob)
            .order_by(BackgroundJob.created_at_utc.desc(), BackgroundJob.job_id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        summaries = [
            {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "status": job.status,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "created_at_utc": job.created_at_utc,
                "updated_at_utc": job.updated_at_utc,
                "error_code": job.error_code,
                "error_message": job.error_message,
            }
            for job in jobs
        ]
        return summaries, total


def cancel_queued_job(engine: Engine, job_id: str) -> dict[str, Any] | None:
    """Cancel a queued job; running work must finish or lose its lease."""
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(BackgroundJob).where(BackgroundJob.job_id == job_id).with_for_update()
        )
        if job is None:
            return None
        if job.status != "queued":
            raise JobStateConflict("only queued jobs can be cancelled")
        job.status = "cancelled"
        job.finished_at_utc = now
        job.updated_at_utc = now
        return serialize_job(job)


def claim_next_job(
    engine: Engine,
    *,
    worker_id: str,
    lease_seconds: int = 60,
) -> ClaimedJob | None:
    """Atomically claim one due job; PostgreSQL workers skip each other's locks."""
    if not worker_id or len(worker_id) > 64:
        raise ValueError("worker_id must contain between 1 and 64 characters")
    if lease_seconds < 3:
        raise ValueError("lease_seconds must be at least 3")
    now = utc_now()
    with Session(engine) as session, session.begin():
        statement = (
            select(BackgroundJob)
            .where(BackgroundJob.status == "queued", BackgroundJob.available_at <= now)
            .order_by(BackgroundJob.available_at, BackgroundJob.created_at_utc, BackgroundJob.job_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = session.scalar(statement)
        if job is None:
            return None
        job.status = "running"
        job.attempt_count += 1
        job.worker_id = worker_id
        job.started_at_utc = job.started_at_utc or now
        job.heartbeat_at_utc = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.updated_at_utc = now
        job.error_code = None
        job.error_message = None
        claimed = ClaimedJob(
            job_id=job.job_id,
            job_type=job.job_type,
            payload=dict(job.payload),
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            worker_id=worker_id,
        )
    return claimed


def heartbeat_job(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_seconds: int = 60,
) -> bool:
    now = utc_now()
    with Session(engine) as session, session.begin():
        result = session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.job_id == job_id,
                BackgroundJob.status == "running",
                BackgroundJob.worker_id == worker_id,
            )
            .values(
                heartbeat_at_utc=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at_utc=now,
            )
        )
        return result.rowcount == 1


def complete_job(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    result_payload: dict[str, Any],
) -> None:
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_id == job_id,
                BackgroundJob.status == "running",
                BackgroundJob.worker_id == worker_id,
            )
            .with_for_update()
        )
        if job is None:
            raise JobStateConflict("worker no longer owns this running job")
        job.status = "succeeded"
        job.result = result_payload
        job.finished_at_utc = now
        job.updated_at_utc = now
        job.heartbeat_at_utc = now
        job.lease_expires_at = None
        job.worker_id = None


def fail_job(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    error_code: str,
    error_type: str,
    retryable: bool,
) -> str:
    """Record a sanitized failure, optionally scheduling a bounded retry."""
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_id == job_id,
                BackgroundJob.status == "running",
                BackgroundJob.worker_id == worker_id,
            )
            .with_for_update()
        )
        if job is None:
            raise JobStateConflict("worker no longer owns this running job")
        can_retry = retryable and job.attempt_count < job.max_attempts
        job.status = "queued" if can_retry else "failed"
        job.error_code = error_code[:64]
        job.error_message = f"Job attempt failed ({error_type[:80]})."
        job.worker_id = None
        job.heartbeat_at_utc = now
        job.lease_expires_at = None
        job.updated_at_utc = now
        if can_retry:
            delay_seconds = min(30 * (2 ** (job.attempt_count - 1)), 300)
            job.available_at = now + timedelta(seconds=delay_seconds)
        else:
            job.finished_at_utc = now
        return job.status


def recover_stale_jobs(engine: Engine) -> dict[str, int]:
    """Requeue expired leases or fail jobs that exhausted all attempts."""
    now = utc_now()
    counts = {"requeued": 0, "failed": 0}
    with Session(engine) as session, session.begin():
        statement = (
            select(BackgroundJob)
            .where(BackgroundJob.status == "running", BackgroundJob.lease_expires_at <= now)
            .order_by(BackgroundJob.lease_expires_at)
            .with_for_update(skip_locked=True)
        )
        for job in session.scalars(statement):
            job.worker_id = None
            job.lease_expires_at = None
            job.heartbeat_at_utc = now
            job.updated_at_utc = now
            job.error_code = "worker_lease_expired"
            job.error_message = "Worker lease expired; job will be retried if attempts remain."
            if job.attempt_count < job.max_attempts:
                job.status = "queued"
                job.available_at = now
                counts["requeued"] += 1
            else:
                job.status = "failed"
                job.finished_at_utc = now
                counts["failed"] += 1
    return counts
