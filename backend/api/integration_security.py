"""Authentication, rate limiting, and durable idempotency for n8n integrations."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from fastapi import Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.persistence.models import IntegrationRequest


def require_integration_key(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """Require the dedicated machine credential, never the approval token."""
    expected = request.app.state.settings.integration_api_key
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail="Integration authentication is not configured")
    scheme, separator, supplied = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status_code=401, detail="A valid integration credential is required")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="A valid integration credential is required")
    return "integration"


class IntegrationRateLimiter:
    """Small per-process limiter; deploy behind a shared gateway for multiple replicas."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        with self._lock:
            window = self._requests[key]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= limit:
                raise HTTPException(
                    status_code=429,
                    detail="Integration rate limit exceeded",
                    headers={"Retry-After": "60"},
                )
            window.append(now)


def enforce_integration_rate_limit(request: Request) -> None:
    limiter: IntegrationRateLimiter = request.app.state.integration_rate_limiter
    client = request.client.host if request.client else "unknown"
    limiter.check(client, request.app.state.settings.integration_rate_limit_per_minute)


def request_hash(operation: str, payload: Any) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_idempotent_response(
    engine: Any,
    *,
    key: str,
    operation: str,
    payload: Any,
) -> dict[str, object] | None:
    digest = request_hash(operation, payload)
    with Session(engine) as session:
        record = session.get(IntegrationRequest, key)
        if record is None:
            return None
        if record.operation != operation or record.request_hash != digest:
            raise HTTPException(status_code=409, detail="Idempotency key is already used for different work")
        return dict(record.response)


def save_idempotent_response(
    engine: Any,
    *,
    key: str,
    operation: str,
    payload: Any,
    response: dict[str, object],
) -> None:
    record = IntegrationRequest(
        idempotency_key=key,
        operation=operation,
        request_hash=request_hash(operation, payload),
        status="completed",
        response=response,
        created_at_utc=datetime.now(UTC),
    )
    with Session(engine) as session:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.get(IntegrationRequest, key)
            if (
                existing is None
                or existing.operation != operation
                or existing.request_hash != record.request_hash
            ):
                raise HTTPException(status_code=409, detail="Idempotency key is already used for different work")
