"""Local, token-protected API for recording inventory-risk review decisions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import Engine

from backend.api.dependencies import get_database_engine
from backend.api.schemas import (
    InventoryRiskApprovalListResponse,
    InventoryRiskApprovalRequest,
    InventoryRiskApprovalResponse,
)
from backend.api.security import require_approval_admin
from backend.persistence.approvals import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    create_inventory_risk_approval,
    list_inventory_risk_approvals,
)


router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.post("", response_model=InventoryRiskApprovalResponse)
def record_inventory_risk_approval(
    request: InventoryRiskApprovalRequest,
    response: Response,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
    actor: str = Depends(require_approval_admin),
    engine: Engine = Depends(get_database_engine),
) -> InventoryRiskApprovalResponse:
    """Append a review event for a saved positive suggested-order quantity."""
    try:
        payload, created = create_inventory_risk_approval(
            engine,
            idempotency_key=idempotency_key,
            risk_run_id=request.risk_run_id,
            series_id=request.series_id,
            decision=request.decision,
            note=request.note,
            actor=actor,
        )
    except ApprovalNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ApprovalConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    response.status_code = 201 if created else 200
    return InventoryRiskApprovalResponse.model_validate(payload)


@router.get("", response_model=InventoryRiskApprovalListResponse)
def get_inventory_risk_approval_history(
    risk_run_id: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    engine: Engine = Depends(get_database_engine),
) -> InventoryRiskApprovalListResponse:
    """Read bounded approval history; this route never changes a saved decision."""
    items, total = list_inventory_risk_approvals(
        engine,
        risk_run_id=risk_run_id,
        limit=limit,
        offset=offset,
    )
    return InventoryRiskApprovalListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
