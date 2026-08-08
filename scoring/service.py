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

from config.loader import get_config
from db import repositories as repo
from db.connection import transaction
from scoring import rules
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
    """Score a single lead and persist the snapshot (or flag).

    The stage comes from the deterministic rules whenever one fires — behavior
    that has been *measured* beats behavior that has been inferred. The model
    still runs (unless mode is rules_only) for the objections and persona the
    decision layer needs, and covers leads no rule matched; in that case its
    stage must clear the configured gates or it is downgraded.
    """
    features = await compute_features(site_id, lead_id)
    rule_hit = rules.evaluate(features, site_id)

    result: Optional[ScoreResult] = None
    error: Optional[str] = None
    if rules.mode(site_id) != "rules_only":
        result, error = await classify(features, site_id)

    stage_source: Optional[str] = None
    stage_reason: Optional[str] = None

    if rule_hit:
        stage = rule_hit["stage"]
        intent = rule_hit["intent_score"]
        stage_source = f"rule: {rule_hit['rule']}"
        stage_reason = " | ".join(rule_hit["evidence"])[:2000]
        if result is not None:
            # Keep the model's colour (objections, persona) under the rule's verdict.
            result = ScoreResult(
                funnel_stage=stage,
                intent_score=intent if intent is not None else result.intent_score,
                likely_objections=result.likely_objections,
                persona_signals=result.persona_signals,
            )
        else:
            result = ScoreResult(
                funnel_stage=stage,
                intent_score=intent if intent is not None else 50,
                likely_objections=[],
                persona_signals={},
            )
            error = None  # a rule decided this lead; the model failure isn't fatal
    elif result is None and rules.mode(site_id) == "rules_only":
        # Rules-only and nothing matched: fall back to the lowest stage rather
        # than blanking the lead — "no rule fired" means no evidence, not unknown.
        stages = get_config("scoring", site_id).get("funnel_stages", ["TOFU", "MOFU", "BOFU"])
        result = ScoreResult(
            funnel_stage=stages[0], intent_score=0, likely_objections=[], persona_signals={},
        )
        stage_source = "no rule matched"
        stage_reason = f"rules-only mode and no rule matched — defaulted to {stages[0]}"
        error = None
    elif result is not None:
        gated, reason = rules.apply_gates(result.funnel_stage, features, site_id)
        stage_source = "model" if reason is None else "model (gated)"
        stage_reason = reason
        if gated != result.funnel_stage:
            result = ScoreResult(
                funnel_stage=gated,
                # An unearned stage shouldn't keep its unearned score either.
                intent_score=min(result.intent_score, 45 if gated == "MOFU" else 20),
                likely_objections=result.likely_objections,
                persona_signals=result.persona_signals,
            )

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
                stage_source=stage_source,
                stage_reason=stage_reason,
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
                stage_source=None,
                stage_reason=None,
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


async def score_pending(site_id: str) -> dict:
    """Materialize new profiles, then score ONLY leads that are new or changed.

    The scheduler calls this each cycle so Gemini fires only for visitors whose
    behavior actually moved, not for the whole table every time.
    """
    profiles_created = await materialize_profiles(site_id)
    async with transaction() as cur:
        lead_ids = await repo.list_leads_needing_score(cur, site_id)

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
        "considered": len(lead_ids),
        "leads_scored": scored,
        "leads_flagged": flagged,
    }
