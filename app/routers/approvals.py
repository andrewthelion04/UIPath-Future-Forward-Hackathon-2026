"""Inboxul de aprobări al hostului."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.container import Container
from app.core.approvals import ApprovalConflict, ApprovalNotFound, InvalidDecision
from app.core.contracts.models import Approval, ApprovalDecisionIn


class ApprovalDecisionOut(BaseModel):
    approval: Approval
    applied: bool  # False dacă aceeași decizie fusese deja înregistrată
    completed: bool | None = None  # doar cu ?wait=true: handlerii au terminat la timp


def build_router(c: Container) -> APIRouter:
    router = APIRouter(tags=["approvals"])

    @router.get("/approvals", response_model=list[Approval])
    async def list_approvals(status: str = "pending") -> list[Approval]:
        """Aprobările în așteptare. `status=all|approved|rejected` pentru istoric."""
        return c.approvals.list(status=status)

    @router.get("/approvals/{approval_id}", response_model=Approval)
    async def get_approval(approval_id: str) -> Approval:
        try:
            return c.approvals.get(approval_id)
        except ApprovalNotFound as exc:
            raise HTTPException(404, f"unknown approval {approval_id}") from exc

    @router.post("/approvals/{approval_id}/decision", response_model=ApprovalDecisionOut)
    async def decide(
        approval_id: str, body: ApprovalDecisionIn, wait: bool = False
    ) -> ApprovalDecisionOut:
        """Aprobă sau respinge. Idempotent: repetarea aceleiași decizii nu are efect nou."""
        try:
            approval, applied = await c.approvals.decide(
                approval_id,
                decision=body.decision,
                option=body.option,
                note=body.note,
                decided_by=body.decided_by,
            )
        except ApprovalNotFound as exc:
            raise HTTPException(404, f"unknown approval {approval_id}") from exc
        except InvalidDecision as exc:
            raise HTTPException(422, str(exc)) from exc
        except ApprovalConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        completed = None
        if wait:
            completed = await c.bus.join(timeout=c.settings.sim_wait_timeout_s)
        return ApprovalDecisionOut(approval=approval, applied=applied, completed=completed)

    return router
