"""Dedicated worker for queued local scenarios and future bounded jobs."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.engine import Engine

from stockwise.api.config import PROJECT_ROOT, get_settings
from stockwise.api.database import create_database_engine
from stockwise.api.schemas import DecisionLabRunRequest
from stockwise.api.decision_routes import _run_scenario
from stockwise.persistence.jobs import (
    ClaimedJob,
    claim_next_job,
    complete_job,
    fail_job,
    heartbeat_job,
    recover_stale_jobs,
)


@dataclass
class PermanentJobError(Exception):
    """A request/data error that should not be retried."""

    code: str
    safe_message: str


def execute_job(job: ClaimedJob) -> dict[str, Any]:
    """Dispatch a registered work type; never accept arbitrary module names."""
    if job.job_type != "decision_lab.run":
        raise PermanentJobError("unsupported_job_type", "The requested job type is not registered.")
    try:
        request = DecisionLabRunRequest.model_validate(job.payload)
        report = _run_scenario(PROJECT_ROOT, request)
    except HTTPException as error:
        raise PermanentJobError(
            f"scenario_http_{error.status_code}",
            "The scenario inputs are unavailable or invalid.",
        ) from error
    except (TypeError, ValueError) as error:
        raise PermanentJobError("invalid_job_payload", "The saved job payload is invalid.") from error
    return {
        "scenario": request.scenario,
        "run_id": str(report.get("run_id", "")),
        "status": str(report.get("status", "unknown")),
        "report": report,
    }


def _heartbeat_until_stopped(
    engine: Engine,
    *,
    job_id: str,
    worker_id: str,
    lease_seconds: int,
    stop_event: threading.Event,
) -> None:
    interval = max(1.0, lease_seconds / 3)
    while not stop_event.wait(interval):
        try:
            if not heartbeat_job(
                engine,
                job_id=job_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            ):
                print(f"Job {job_id}: heartbeat ownership lost.", file=sys.stderr)
                return
        except Exception as error:  # pragma: no cover - transient database fault
            print(f"Job {job_id}: heartbeat failed ({type(error).__name__}).", file=sys.stderr)


def run_one_job(engine: Engine, *, worker_id: str, lease_seconds: int = 30) -> bool:
    """Recover expired leases, claim one available job, and execute it once."""
    recovered = recover_stale_jobs(engine)
    if recovered["requeued"] or recovered["failed"]:
        print(f"Recovered stale jobs: {recovered}")

    job = claim_next_job(engine, worker_id=worker_id, lease_seconds=lease_seconds)
    if job is None:
        return False
    print(f"Claimed job {job.job_id} ({job.job_type}), attempt {job.attempt_count}/{job.max_attempts}.")

    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_until_stopped,
        kwargs={
            "engine": engine,
            "job_id": job.job_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = execute_job(job)
        complete_job(engine, job_id=job.job_id, worker_id=worker_id, result_payload=result)
        print(f"Job {job.job_id}: succeeded.")
    except PermanentJobError as error:
        status = fail_job(
            engine,
            job_id=job.job_id,
            worker_id=worker_id,
            error_code=error.code,
            error_type=type(error).__name__,
            retryable=False,
        )
        print(f"Job {job.job_id}: {status} ({error.code}).")
    except Exception as error:  # pragma: no cover - exercised via handler fault injection
        status = fail_job(
            engine,
            job_id=job.job_id,
            worker_id=worker_id,
            error_code="job_execution_failed",
            error_type=type(error).__name__,
            retryable=True,
        )
        print(f"Job {job.job_id}: {status} after {type(error).__name__}.", file=sys.stderr)
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=lease_seconds)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the StockWise PostgreSQL job worker.")
    parser.add_argument("--once", action="store_true", help="claim and process at most one job, then exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="idle polling interval; default 1 second")
    parser.add_argument("--lease-seconds", type=int, default=30, help="worker lease duration; minimum 3 seconds")
    args = parser.parse_args()
    if args.poll_seconds < 0.1 or args.lease_seconds < 3:
        parser.error("poll-seconds must be at least 0.1 and lease-seconds at least 3")

    database_url = get_settings().database_url
    if not database_url:
        print("STOCKWISE_DATABASE_URL is not configured.", file=sys.stderr)
        return 2
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    engine = create_database_engine(database_url)
    try:
        if args.once:
            run_one_job(engine, worker_id=worker_id, lease_seconds=args.lease_seconds)
            return 0
        print(f"StockWise worker started: {worker_id}")
        while True:
            processed = run_one_job(engine, worker_id=worker_id, lease_seconds=args.lease_seconds)
            if not processed:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("StockWise worker stopped.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
