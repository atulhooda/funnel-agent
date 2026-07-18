"""Pydantic / dataclass models for the decision payload and guardrail result."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Decision(BaseModel):
    """A proposed next action for a lead (the model's Stage output)."""

    action: str
    channel: Optional[str] = None
    message: Optional[str] = None
    send_at: Optional[datetime] = None
    reasoning: str = ""


@dataclass
class GuardrailResult:
    passed: bool
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "violations": self.violations}


class DecisionOutcome(BaseModel):
    """What /decide returns per lead."""

    site_id: str
    lead_id: int
    decision_id: Optional[int] = None
    action: str
    status: str                       # accepted | rejected
    channel: Optional[str] = None
    send_at: Optional[datetime] = None
    violations: list[str] = []
    reasoning: str = ""


class DecideRunSummary(BaseModel):
    site_id: str
    total_leads: int
    accepted: int
    rejected: int
    by_action: dict[str, int]
