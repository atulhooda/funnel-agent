"""Layer 1 — HTTP routes: POST /track and POST /identify."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from deps import get_site_id, require_write_key
from tracking import service
from tracking.geo import client_ip_from_headers
from tracking.schemas import (
    IdentifyRequest,
    IdentifyResponse,
    LocateRequest,
    LocateResponse,
    TrackRequest,
    TrackResponse,
)

router = APIRouter(tags=["tracking"])


@router.post("/track", response_model=TrackResponse, dependencies=[Depends(require_write_key)])
async def track(body: TrackRequest, request: Request, site_id: str = Depends(get_site_id)) -> TrackResponse:
    client_ip = client_ip_from_headers(
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-real-ip"),
        request.client.host if request.client else None,
        request.headers.get("cf-connecting-ip"),
    )
    client_tz = body.metadata.get("tz") if isinstance(body.metadata, dict) else None
    result = await service.track_event(
        site_id=site_id,
        event_type=body.event_type,
        url=body.url,
        timestamp=body.timestamp,
        anonymous_id=body.anonymous_id,
        session_id=body.session_id,
        metadata=body.metadata,
        client_ip=client_ip,
        client_tz=client_tz,
    )
    return TrackResponse(
        site_id=site_id,
        event_id=result["event_id"],
        lead_id=result["lead_id"],
        page_type=result["page_type"],
    )


@router.post("/locate", response_model=LocateResponse, dependencies=[Depends(require_write_key)])
async def locate(body: LocateRequest, site_id: str = Depends(get_site_id)) -> LocateResponse:
    """Record a precise location the visitor explicitly consented to share.

    Only ever called from `funnel.locate()`, which the browser gates behind its
    own permission prompt — this endpoint cannot obtain a location by itself.
    """
    result = await service.record_precise_location(
        site_id=site_id,
        anonymous_id=body.anonymous_id,
        latitude=body.latitude,
        longitude=body.longitude,
        accuracy_m=body.accuracy_m,
    )
    return LocateResponse(
        site_id=site_id,
        anonymous_id=result["anonymous_id"],
        accuracy_m=result["accuracy_m"],
    )


@router.post("/identify", response_model=IdentifyResponse, dependencies=[Depends(require_write_key)])
async def identify(body: IdentifyRequest, site_id: str = Depends(get_site_id)) -> IdentifyResponse:
    result = await service.identify(
        site_id=site_id,
        anonymous_id=body.anonymous_id,
        name=body.name,
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
