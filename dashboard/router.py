"""Layer 5 — read-only dashboard.

JSON routes plus one minimal HTML page (which consumes those JSON routes):
  GET /dashboard            the HTML page
  GET /api/overview         summary counts
  GET /api/leads            all leads: stage, intent, consent status
  GET /api/decisions        the decision log with reasoning + guardrail result
  GET /api/sent-messages    the stubbed sent-messages log
Nothing here writes.
"""
from __future__ import annotations

import pathlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from db import repositories as repo
from db.connection import transaction
from deps import get_site_id

router = APIRouter(tags=["dashboard"])
TEMPLATE = pathlib.Path(__file__).resolve().parent / "templates" / "dashboard.html"

LIVE_WINDOW_SECONDS = 45   # a visitor counts as "live" if seen within this window
DWELL_CAP_SECONDS = 1800   # cap a single page's counted dwell so idle time isn't over-counted


def _path(url):
    if not url:
        return "/"
    try:
        p = urlparse(url)
        return (p.path or "/") + (("?" + p.query) if p.query else "")
    except Exception:
        return url


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))


@router.get("/api/leads")
async def api_leads(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "leads": await repo.list_leads(cur, site_id)}


@router.get("/api/decisions")
async def api_decisions(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "decisions": await repo.list_decisions(cur, site_id)}


@router.get("/api/sent-messages")
async def api_sent_messages(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "sent_messages": await repo.list_sent_messages(cur, site_id)}


@router.get("/api/lead/{lead_id}/journey")
async def api_lead_journey(lead_id: int, site_id: str = Depends(get_site_id)) -> dict:
    """Full journey for one lead: every page + click with timings, dwell, totals."""
    async with transaction() as cur:
        lead = await repo.get_lead_by_id(cur, site_id, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="lead not found")
        events = await repo.list_events_for_lead(cur, site_id, lead_id)
        last_presence = await repo.lead_last_presence(cur, site_id, lead_id)
        location = await repo.lead_location(cur, site_id, lead_id)

    n = len(events)
    first = events[0]["occurred_at"] if events else None
    last_event_time = events[-1]["occurred_at"] if events else None
    # "Last active" = the latest of their last event or last heartbeat, so the
    # final page's dwell and the total never go negative on clock skew.
    _cands = [t for t in (last_presence, last_event_time) if t is not None]
    last_activity = max(_cands) if _cands else None

    timeline = []
    for i, e in enumerate(events):
        start = e["occurred_at"]
        end = events[i + 1]["occurred_at"] if i + 1 < n else (last_activity or start)
        dwell = max(0.0, (end - start).total_seconds())
        timeline.append({
            "event_type": e["event_type"],
            "url": e["url"],
            "path": _path(e["url"]),
            "page_type": e["page_type"],
            "session_id": e["session_id"],
            "metadata": e["metadata"] or {},
            "occurred_at": e["occurred_at"].isoformat(),
            "dwell_seconds": round(dwell) if e["event_type"] == "page_view" else None,
        })

    pageviews = [e for e in events if e["event_type"] == "page_view"]
    clicks = [e for e in events if e["event_type"] != "page_view"]
    total_time = max(0.0, (last_activity - first).total_seconds()) if (first and last_activity) else 0
    active_time = sum(min(t["dwell_seconds"] or 0, DWELL_CAP_SECONDS) for t in timeline)
    page_counts = Counter(_path(e["url"]) for e in pageviews)

    return {
        "site_id": site_id,
        "lead_id": lead_id,
        "summary": {
            "funnel_stage": lead.get("funnel_stage"),
            "intent_score": lead.get("intent_score"),
            "name": lead.get("name"),
            "email": lead.get("email"),
            "country": location.get("country"),
            "region": location.get("region"),
            "city": location.get("city"),
            "timezone": location.get("timezone"),
            "events": n,
            "pageviews": len(pageviews),
            "clicks": len(clicks),
            "sessions": len({e["session_id"] for e in events if e["session_id"]}),
            "pages_distinct": len(page_counts),
            "first_seen": first.isoformat() if first else None,
            "last_seen": last_activity.isoformat() if last_activity else None,
            "total_time_seconds": round(total_time),
            "active_time_seconds": round(active_time),
            "top_pages": page_counts.most_common(8),
        },
        "timeline": timeline,
    }


@router.get("/api/live")
async def api_live(site_id: str = Depends(get_site_id)) -> dict:
    """Visitors currently on the site (seen within LIVE_WINDOW_SECONDS)."""
    since = datetime.now(timezone.utc) - timedelta(seconds=LIVE_WINDOW_SECONDS)
    async with transaction() as cur:
        visitors = await repo.list_active_visitors(cur, site_id, since)
    return {
        "site_id": site_id,
        "window_seconds": LIVE_WINDOW_SECONDS,
        "active": len(visitors),
        "visitors": visitors,
    }


@router.get("/api/overview")
async def api_overview(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        leads = await repo.list_leads(cur, site_id)
        decisions = await repo.list_decisions(cur, site_id)
        sent = await repo.list_sent_messages(cur, site_id)
    return {
        "site_id": site_id,
        "overview": {
            "leads": len(leads),
            "by_stage": dict(Counter((lead["funnel_stage"] or "unscored") for lead in leads)),
            "decisions": len(decisions),
            "decisions_by_status": dict(Counter(d["status"] for d in decisions)),
            "decisions_by_action": dict(Counter(d["action"] for d in decisions)),
            "sent": sum(1 for s in sent if s["status"] == "sent"),
            "skipped": sum(1 for s in sent if s["status"] == "skipped"),
        },
    }
