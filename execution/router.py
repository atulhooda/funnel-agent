"""Layer 4 — execution trigger routes (stubbed sends).

POST /execute/run              execute all pending accepted outreach decisions
POST /execute/decision/{id}    execute a single decision
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from deps import get_site_id
from execution import service

router = APIRouter(tags=["execution"])


@router.post("/execute/run")
async def execute_run(site_id: str = Depends(get_site_id)) -> dict:
    return await service.execute_pending(site_id)


@router.post("/execute/decision/{decision_id}")
async def execute_one(decision_id: int, site_id: str = Depends(get_site_id)) -> dict:
    outcome = await service.execute_decision(site_id, decision_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
    return outcome
