"""Pydantic request/response models for /track and /identify (Layer 1)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class TrackRequest(BaseModel):
    event_type: str
    anonymous_id: str
    url: Optional[str] = None
    timestamp: Optional[datetime] = None          # client event time; defaults to server now
    session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrackResponse(BaseModel):
    status: str = "ok"
    site_id: str
    event_id: Optional[int] = None                # None for heartbeats (presence-only, no event stored)
    lead_id: Optional[int] = None                 # set when the anonymous_id is already identified
    page_type: Optional[str] = None               # resolved from config


class LocateRequest(BaseModel):
    """A consented browser Geolocation fix (navigator.geolocation)."""
    anonymous_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)   # the browser's own radius


class LocateResponse(BaseModel):
    status: str = "ok"
    site_id: str
    anonymous_id: str
    accuracy_m: Optional[int] = None


class IdentifyRequest(BaseModel):
    # Optional: it links the person to what they browsed beforehand, but a lead
    # who verified their phone must be recorded whether or not that link exists.
    # A blocked tracker or a fresh private window means no cookie, and losing the
    # contact details in that case is far worse than losing the history.
    anonymous_id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    # Optional on purpose: omitted means "not stated here, leave it alone".
    # Defaulting these to False made any partial identify — updating a phone,
    # confirming an OTP — silently revoke consent granted earlier.
    email_opt_in: Optional[bool] = None
    whatsapp_opt_in: Optional[bool] = None
    consent_timestamp: Optional[datetime] = None  # when consent was given; defaults to server now
    consent_source: Optional[str] = None
    # Set true only after the number passed an OTP check. Asserting ownership is
    # not the same as consent, so this never grants whatsapp_opt_in on its own.
    # Honoured only for callers holding the server key — see require_server_key.
    phone_verified: bool = False

    @model_validator(mode="after")
    def _require_contact(self) -> "IdentifyRequest":
        if not self.email and not self.phone:
            raise ValueError("identify requires at least one of: email, phone")
        return self


class IdentifyResponse(BaseModel):
    status: str = "ok"
    site_id: str
    lead_id: int
    created: bool                                 # True if a new lead was created
    anonymous_id: Optional[str] = None            # absent when no tracking cookie was present
    backfilled_events: int                        # prior events re-pointed to the lead
    phone_verified: bool = False                  # whether the lead now holds a verified number
