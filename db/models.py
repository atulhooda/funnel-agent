"""Lightweight row types for the tables in schema.sql.

Plain TypedDicts for readability/documentation — repositories return dict rows
(psycopg dict_row), no ORM. `total=False` because reads may select subsets.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, TypedDict


class IdentityRow(TypedDict, total=False):
    id: int
    site_id: str
    anonymous_id: str
    lead_id: Optional[int]


class LeadRow(TypedDict, total=False):
    id: int
    site_id: str
    email: Optional[str]
    phone: Optional[str]
    email_opt_in: bool
    whatsapp_opt_in: bool
    consent_timestamp: Optional[datetime]
    consent_source: Optional[str]
    funnel_stage: Optional[str]
    intent_score: Optional[int]
    likely_objections: Any
    persona_signals: Any
    scored_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class EventRow(TypedDict, total=False):
    id: int
    site_id: str
    anonymous_id: str
    lead_id: Optional[int]
    event_type: str
    url: Optional[str]
    page_type: Optional[str]
    session_id: Optional[str]
    metadata: Any
    occurred_at: datetime
    received_at: datetime
