"""Append-only review history for persisted inventory-risk recommendations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.persistence.models import InventoryRiskApproval, InventoryRiskItem, InventoryRiskRun


ApprovalDecision = Literal["approved", "rejected", "revision_requested"]


class ApprovalNotFoundError(ValueError):
    """The saved risk run or series does not exist."""


class ApprovalConflictError(ValueError):
    """The request conflicts with the saved recommendation or idempotency key."""


def _approval_payload(record: InventoryRiskApproval) -> dict:
    """Copy response fields while the database session is still open."""
    return {
        "approval_id": record.approval_id,
        "created_at_utc": record.created_at_utc,
        "risk_run_id": record.risk_run_id,
        "series_id": record.series_id,
        "decision": record.decision,
        "note": record.note,
        "recommended_order_quantity_units": record.recommended_order_quantity_units,
        "stockout_probability": record.stockout_probability,
        "inventory_provenance": record.inventory_provenance,
        "actor": record.actor,
    }


def _same_request(
    record: InventoryRiskApproval,
    *,
    risk_run_id: str,
    series_id: str,
    decision: ApprovalDecision,
    note: str | None,
) -> bool:
    return (
        record.risk_run_id == risk_run_id
        and record.series_id == series_id
        and record.decision == decision
        and record.note == note
    )


def create_inventory_risk_approval(
    engine: Engine,
    *,
    idempotency_key: str,
    risk_run_id: str,
    series_id: str,
    decision: ApprovalDecision,
    note: str | None,
    actor: str = "local_admin",
) -> tuple[dict, bool]:
    """Record a decision against an existing positive suggested order, without acting on it.

    Reusing an idempotency key for the same request returns the original event. Reusing
    it for different content is rejected. A new key always creates a new history event.
    """
    try:
        with Session(engine) as session:
            with session.begin():
                existing = session.scalar(
                    select(InventoryRiskApproval).where(
                        InventoryRiskApproval.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    if not _same_request(
                        existing,
                        risk_run_id=risk_run_id,
                        series_id=series_id,
                        decision=decision,
                        note=note,
                    ):
                        raise ApprovalConflictError(
                            "Idempotency key was already used for a different approval request"
                        )
                    return _approval_payload(existing), False

                run = session.get(InventoryRiskRun, risk_run_id)
                if run is None:
                    raise ApprovalNotFoundError("Saved risk run or recommendation not found")
                item = session.get(InventoryRiskItem, (risk_run_id, series_id))
                if item is None:
                    raise ApprovalNotFoundError("Saved risk run or recommendation not found")
                if item.suggested_order_quantity_units <= 0:
                    raise ApprovalConflictError(
                        "This risk item has no positive replenishment recommendation to review"
                    )

                record = InventoryRiskApproval(
                    approval_id=uuid.uuid4().hex,
                    idempotency_key=idempotency_key,
                    created_at_utc=datetime.now(timezone.utc),
                    risk_run_id=risk_run_id,
                    series_id=series_id,
                    decision=decision,
                    note=note,
                    recommended_order_quantity_units=item.suggested_order_quantity_units,
                    stockout_probability=item.stockout_probability,
                    inventory_provenance=run.inventory_provenance,
                    actor=actor,
                )
                session.add(record)
                session.flush()
                result = _approval_payload(record)
        return result, True
    except IntegrityError:
        # A concurrent retry may win the unique-key insert after our initial lookup.
        with Session(engine) as session:
            existing = session.scalar(
                select(InventoryRiskApproval).where(
                    InventoryRiskApproval.idempotency_key == idempotency_key
                )
            )
            if existing is None:
                raise
            if not _same_request(
                existing,
                risk_run_id=risk_run_id,
                series_id=series_id,
                decision=decision,
                note=note,
            ):
                raise ApprovalConflictError(
                    "Idempotency key was already used for a different approval request"
                )
            return _approval_payload(existing), False


def list_inventory_risk_approvals(
    engine: Engine,
    *,
    limit: int,
    offset: int,
    risk_run_id: str | None = None,
) -> tuple[list[dict], int]:
    """Return bounded newest-first approval history, optionally for one risk run."""
    with Session(engine) as session:
        query = select(InventoryRiskApproval)
        count_query = select(func.count()).select_from(InventoryRiskApproval)
        if risk_run_id is not None:
            query = query.where(InventoryRiskApproval.risk_run_id == risk_run_id)
            count_query = count_query.where(InventoryRiskApproval.risk_run_id == risk_run_id)
        total = session.scalar(count_query) or 0
        records = session.scalars(
            query.order_by(
                InventoryRiskApproval.created_at_utc.desc(),
                InventoryRiskApproval.approval_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        items = [_approval_payload(record) for record in records]
    return items, total
