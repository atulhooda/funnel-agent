"""Layer 2 orchestration.

- ensure_profile_lead: materialize a lightweight profile lead for an anonymous
  visitor (the "all visitors" model — everyone flows through the brain).
- score_lead: Stage A features -> Stage B classification -> persist snapshot
  (or flag on failure).
- score_all: materialize profiles for every unlinked visitor, then score every
  lead for the site.

DB transactions are kept short and never span the Gemini call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from db import repositories as repo
from db.connection import transaction
from scoring.classifier import classify
from scoring.features import compute_features
from scoring.schemas import ScoreResult


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_profile_lead(site_id: str, anonymous_id: str) -> tuple[int, bool]:
    """Return (lead_id, created). Creates + links an anonymous profile lead and
    backfills its events if the visitor has none yet."""
    async with transaction() as cur:
        identity = await repo.get_or_create_identity(cur, site_id, anonymous_id)
        if identity.get("lead_id"):
            return identity["lead_id"], False
        lead = await repo.create_anonymous_lead(cur, site_id)
        await repo.link_identity(cur, site_id, anonymous_id, lead["id"])
        await repo.backfill_events_to_lead(cur, site_id, anonymous_id, lead["id"])
        return lead["id"], True


async def materialize_profiles(site_id: str) -> int:
    """Create profile leads for every anonymous_id not yet linked. Returns count."""
    async with transaction() as cur:
        anon_ids = await repo.list_unlinked_anonymous_ids(cur, site_id)
    created = 0
    for anon_id in anon_ids:
        _lead_id, was_created = await ensure_profile_lead(site_id, anon_id)
        created += int(was_created)
    return created


async def score_lead(site_id: str, lead_id: int) -> tuple[Optional[ScoreResult], Optional[str]]:
    """Score a single lead and persist the snapshot (or flag)."""
    features = await compute_features(site_id, lead_id)
    result, error = await classify(features, site_id)
    scored_at = _utcnow()

    async with transaction() as cur:
        if result is not None:
            await repo.update_lead_score(
                cur,
                lead_id=lead_id,
                funnel_stage=result.funnel_stage,
                intent_score=result.intent_score,
                likely_objections=result.likely_objections,
                persona_signals=result.persona_signals,
                scored_at=scored_at,
                scoring_error=None,
            )
        else:
            await repo.update_lead_score(
                cur,
                lead_id=lead_id,
                funnel_stage=None,
                intent_score=None,
                likely_objections=[],
                persona_signals={},
                scored_at=scored_at,
                scoring_error=error,
            )
    return result, error


async def score_all(site_id: str) -> dict:
    """Materialize profiles for all visitors, then score every lead."""
    profiles_created = await materialize_profiles(site_id)
    async with transaction() as cur:
        lead_ids = await repo.list_lead_ids(cur, site_id)

    scored = flagged = 0
    for lead_id in lead_ids:
        result, _error = await score_lead(site_id, lead_id)
        if result is not None:
            scored += 1
        else:
            flagged += 1

    return {
        "site_id": site_id,
        "profiles_created": profiles_created,
        "leads_scored": scored,
        "leads_flagged": flagged,
        "total_leads": len(lead_ids),
    }
