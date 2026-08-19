"""Layer 3 orchestration.

For each lead: load profile + behavior + recent-outreach count -> ask the engine
for a next action -> validate with guardrails -> log EVERY decision (accepted or
rejected, with full reasoning + guardrail result) to the `decisions` table.

If the engine can't produce a valid decision, we fall back to a handoff_human
proposal (which still passes through guardrails and is logged). Accepted sends
are handed to execution in Layer 4; this layer only decides + logs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from config.loader import get_config
from config.settings import get_settings
from db import repositories as repo
from db.connection import transaction
from decision import engine, guardrails
from decision.schemas import Decision, DecisionOutcome
from scoring.features import compute_features


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def decide_for_lead(site_id: str, lead_id: int) -> Optional[DecisionOutcome]:
    gcfg = get_config("guardrails", site_id)
    templates = get_config("templates", site_id)
    now = _utcnow()
    window_days = int(gcfg.get("rate_limit", {}).get("window_days", 7))
    since = now - timedelta(days=window_days)
    outreach_actions = gcfg.get("outreach_actions", [])

    async with transaction() as cur:
        lead = await repo.get_lead_by_id(cur, site_id, lead_id)
        if lead is None:
            return None
        recent_outreach = await repo.count_recent_outreach(cur, site_id, lead_id, outreach_actions, since)
        nudged_stages = await repo.stages_already_nudged(cur, site_id, lead_id)

    features = await compute_features(site_id, lead_id)
    proposed, engine_error, raw = await engine.decide(
        lead, features, gcfg, templates, now, recent_outreach, site_id
    )

    if proposed is None:
        proposed = Decision(
            action="handoff_human",
            reasoning=f"Fallback: decision engine could not produce a valid decision ({engine_error}).",
        )
        raw = None

    result = guardrails.validate(proposed, lead, recent_outreach, now, gcfg, nudged_stages)
    status = "accepted" if result.passed else "rejected"

    async with transaction() as cur:
        row = await repo.insert_decision(
            cur,
            site_id=site_id,
            lead_id=lead_id,
            action=proposed.action,
            channel=proposed.channel,
            message=proposed.message,
            send_at=proposed.send_at,
            reasoning=proposed.reasoning,
            status=status,
            guardrail_result=result.to_dict(),
            model=get_settings().gemini_model,
            raw_response=raw,
        )

    return DecisionOutcome(
        site_id=site_id,
        lead_id=lead_id,
        decision_id=row["id"],
        action=proposed.action,
        status=status,
        channel=proposed.channel,
        send_at=proposed.send_at,
        violations=result.violations,
        reasoning=proposed.reasoning or "",
    )


async def decide_all(site_id: str) -> dict:
    async with transaction() as cur:
        lead_ids = await repo.list_lead_ids(cur, site_id)
    return await _decide_for_ids(site_id, lead_ids)


async def decide_pending(site_id: str) -> dict:
    """Decide ONLY leads that were (re)scored after their last decision.

    Called by the scheduler each cycle — avoids re-deciding stable leads (which
    would spam the decision log and Gemini) while still reacting when a visitor's
    stage changes (e.g. TOFU -> BOFU after they hit /pricing).
    """
    async with transaction() as cur:
        lead_ids = await repo.list_leads_needing_decision(cur, site_id)
    return await _decide_for_ids(site_id, lead_ids)


async def _decide_for_ids(site_id: str, lead_ids: list[int]) -> dict:
    accepted = rejected = 0
    by_action: dict[str, int] = {}
    for lead_id in lead_ids:
        outcome = await decide_for_lead(site_id, lead_id)
        if outcome is None:
            continue
        by_action[outcome.action] = by_action.get(outcome.action, 0) + 1
        if outcome.status == "accepted":
            accepted += 1
        else:
            rejected += 1

    return {
        "site_id": site_id,
        "total_leads": len(lead_ids),
        "accepted": accepted,
        "rejected": rejected,
        "by_action": by_action,
    }
