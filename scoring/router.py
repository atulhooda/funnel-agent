"""Layer 2 — scoring trigger routes.

POST /score/run          materialize profiles for all visitors, then score every lead
POST /score/lead/{id}    score a single lead
These drive the two-stage scoring; the dashboard (Layer 5) reads the results.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from deps import get_site_id
from scoring import service
from scoring.schemas import ScoreRunSummary

router = APIRouter(tags=["scoring"])


@router.post("/score/run", response_model=ScoreRunSummary)
async def score_run(site_id: str = Depends(get_site_id)) -> ScoreRunSummary:
    summary = await service.score_all(site_id)
    return ScoreRunSummary(**summary)


@router.post("/score/lead/{lead_id}")
async def score_one(lead_id: int, site_id: str = Depends(get_site_id)) -> dict:
    result, error = await service.score_lead(site_id, lead_id)
    return {
        "site_id": site_id,
        "lead_id": lead_id,
        "scored": result is not None,
        "error": error,
        "result": result.model_dump() if result is not None else None,
    }
