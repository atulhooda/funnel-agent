"""Layer 1 — HTTP routes: POST /track and POST /identify."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from deps import get_site_id
from tracking import service
from tracking.schemas import (
    IdentifyRequest,
    IdentifyResponse,
    TrackRequest,
    TrackResponse,
)

router = APIRouter(tags=["tracking"])


@router.post("/track", response_model=TrackResponse)
async def track(body: TrackRequest, site_id: str = Depends(get_site_id)) -> TrackResponse:
    result = await service.track_event(
        site_id=site_id,
        event_type=body.event_type,
        url=body.url,
        timestamp=body.timestamp,
        anonymous_id=body.anonymous_id,
        session_id=body.session_id,
        metadata=body.metadata,
    )
    return TrackResponse(
        site_id=site_id,
        event_id=result["event_id"],
        lead_id=result["lead_id"],
        page_type=result["page_type"],
    )


@router.post("/identify", response_model=IdentifyResponse)
async def identify(body: IdentifyRequest, site_id: str = Depends(get_site_id)) -> IdentifyResponse:
    result = await service.identify(
        site_id=site_id,
        anonymous_id=body.anonymous_id,
        email=body.email,
        phone=body.phone,
        email_opt_in=body.email_opt_in,
        whatsapp_opt_in=body.whatsapp_opt_in,
        consent_timestamp=body.consent_timestamp,
        consent_source=body.consent_source,
    )
    return IdentifyResponse(
        site_id=site_id,
        lead_id=result["lead_id"],
        created=result["created"],
        anonymous_id=result["anonymous_id"],
        backfilled_events=result["backfilled_events"],
    )
