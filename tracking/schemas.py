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
    event_id: int
    lead_id: Optional[int] = None                 # set when the anonymous_id is already identified
    page_type: Optional[str] = None               # resolved from config


class IdentifyRequest(BaseModel):
    anonymous_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    email_opt_in: bool = False
    whatsapp_opt_in: bool = False
    consent_timestamp: Optional[datetime] = None  # when consent was given; defaults to server now
    consent_source: Optional[str] = None

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
    anonymous_id: str
    backfilled_events: int                        # prior events re-pointed to the lead
