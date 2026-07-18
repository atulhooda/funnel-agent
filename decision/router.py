"""Layer 3 — decision trigger routes.

POST /decide/run          decide a next action for every lead (each logged)
POST /decide/lead/{id}    decide for a single lead
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from decision import service
from decision.schemas import DecideRunSummary, DecisionOutcome
from deps import get_site_id

router = APIRouter(tags=["decision"])


@router.post("/decide/run", response_model=DecideRunSummary)
async def decide_run(site_id: str = Depends(get_site_id)) -> DecideRunSummary:
    summary = await service.decide_all(site_id)
    return DecideRunSummary(**summary)


@router.post("/decide/lead/{lead_id}", response_model=DecisionOutcome)
async def decide_one(lead_id: int, site_id: str = Depends(get_site_id)) -> DecisionOutcome:
    outcome = await service.decide_for_lead(site_id, lead_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail=f"lead {lead_id} not found")
    return outcome
