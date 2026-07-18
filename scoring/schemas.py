"""Pydantic models for Stage A features and the Stage B scoring output."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ScoreResult(BaseModel):
    """Validated Stage B output stored on the lead."""

    funnel_stage: str
    intent_score: int
    likely_objections: list[str] = Field(default_factory=list)
    persona_signals: dict[str, Any] = Field(default_factory=dict)


class ScoreRunSummary(BaseModel):
    site_id: str
    profiles_created: int
    leads_scored: int
    leads_flagged: int
    total_leads: int
