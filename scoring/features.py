"""Layer 2, Stage A — deterministic features (SQL + Python).

Computes visit/session counts, pages-by-type and by funnel lean, recency, source
and high-intent-event counts for a lead from the `events` table. Page-type leans
come from config (config/page_types.yaml), not hardcoded. Output is a plain,
JSON-serializable dict handed to Stage B.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from config.loader import get_config, page_type_leans
from db import repositories as repo
from db.connection import transaction


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hours_since(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return round((_utcnow() - dt).total_seconds() / 3600.0, 1)


def _days_since(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return round((_utcnow() - dt).total_seconds() / 86400.0, 1)


def _source_from_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Optional[str]]:
    md = metadata or {}
    return {
        "referrer": md.get("referrer"),
        "utm_source": md.get("utm_source"),
        "utm_campaign": md.get("utm_campaign"),
    }


async def compute_features(site_id: str, lead_id: int) -> dict[str, Any]:
    """Assemble the Stage A feature summary for one lead."""
    cfg = get_config("scoring", site_id)
    high_intent = cfg.get("high_intent_events", [])
    recent_limit = int(cfg.get("recent_events_limit", 20))
    leans = page_type_leans(site_id)

    async with transaction() as cur:
        lead = await repo.get_lead_by_id(cur, site_id, lead_id)
        agg = await repo.event_aggregates(cur, site_id, lead_id)
        by_type = await repo.event_counts_by_page_type(cur, site_id, lead_id)
        high_intent_counts = await repo.event_counts_for_types(cur, site_id, lead_id, high_intent)
        first = await repo.first_event(cur, site_id, lead_id)
        recent = await repo.recent_events(cur, site_id, lead_id, recent_limit)

    lead = lead or {}
    identified = bool(lead.get("email") or lead.get("phone"))

    # Roll page-type counts up to funnel leans (TOFU/MOFU/BOFU).
    by_lean: dict[str, int] = {}
    for page_type, count in by_type.items():
        lean = leans.get(page_type)
        if lean:
            by_lean[lean] = by_lean.get(lean, 0) + count

    recent_events = [
        {
            "event_type": ev["event_type"],
            "page_type": ev.get("page_type"),
            "url": ev.get("url"),
            "session_id": ev.get("session_id"),
            "hours_ago": _hours_since(ev.get("occurred_at")),
        }
        for ev in recent
    ]

    return {
        "identified": identified,
        "totals": {
            "events": agg.get("events", 0),
            "sessions": agg.get("sessions", 0),
            "pageviews": agg.get("pageviews", 0),
            "active_days": agg.get("active_days", 0),
        },
        "pages_by_type": by_type,
        "pages_by_lean": by_lean,
        "high_intent_events": high_intent_counts,
        "recency": {
            "last_event_hours_ago": _hours_since(agg.get("last_at")),
            "first_event_days_ago": _days_since(agg.get("first_at")),
        },
        "source": _source_from_metadata(first.get("metadata") if first else None),
        "recent_events": recent_events,
    }
