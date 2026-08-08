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

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from config import loader
from config.loader import effective_execution_mode, get_config
from config.settings import get_settings
from db import repositories as repo
from db.connection import transaction
from deps import get_site_id

router = APIRouter(tags=["dashboard"])
_TPL_DIR = pathlib.Path(__file__).resolve().parent / "templates"
TEMPLATE = _TPL_DIR / "dashboard.html"
INSIGHTS_TEMPLATE = _TPL_DIR / "insights.html"
MAP_TEMPLATE = _TPL_DIR / "map.html"
SETTINGS_TEMPLATE = _TPL_DIR / "settings.html"

LIVE_WINDOW_SECONDS = 24   # "live" if seen within this window (≈2.4× the 10s heartbeat, so no flicker)
INSIGHTS_DAYS = 14         # trend window for the insights page
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


@router.get("/insights", response_class=HTMLResponse, include_in_schema=False)
async def insights_page() -> HTMLResponse:
    return HTMLResponse(INSIGHTS_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/api/insights")
async def api_insights(site_id: str = Depends(get_site_id)) -> dict:
    """Page analytics: totals, per-page views, page-type split, and a daily trend."""
    async with transaction() as cur:
        totals = await repo.insights_totals(cur, site_id)
        pages = await repo.page_views_breakdown(cur, site_id)
        page_types = await repo.page_type_breakdown(cur, site_id)
        by_day_rows = await repo.views_by_day(cur, site_id, INSIGHTS_DAYS)

    # fill missing days so the trend line is continuous
    today = datetime.now(timezone.utc).date()
    seen = {r["day"]: r for r in by_day_rows}
    by_day = []
    for i in range(INSIGHTS_DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = seen.get(d)
        by_day.append({"day": d, "views": (row["views"] if row else 0),
                       "visitors": (row["visitors"] if row else 0)})

    return {
        "site_id": site_id,
        "totals": totals,
        "pages": pages,
        "page_types": page_types,
        "by_day": by_day,
    }


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


@router.get("/map", response_class=HTMLResponse, include_in_schema=False)
async def map_page() -> HTMLResponse:
    return HTMLResponse(MAP_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page() -> HTMLResponse:
    return HTMLResponse(SETTINGS_TEMPLATE.read_text(encoding="utf-8"))


def _settings_view(site_id: str) -> dict:
    g = get_config("guardrails", site_id)
    rl = g.get("rate_limit", {})
    sw = g.get("send_window", {})
    pt = get_config("page_types", site_id)
    s = get_settings()
    return {
        "execution_mode": effective_execution_mode(),
        "guardrails": {
            "timezone": g.get("timezone", "UTC"),
            "max_outreach": rl.get("max_outreach", 2),
            "window_days": rl.get("window_days", 7),
            "send_start_hour": sw.get("start_hour", 9),
            "send_end_hour": sw.get("end_hour", 21),
        },
        "scheduler": {"enabled": s.scheduler_enabled, "interval_seconds": s.scheduler_interval_seconds},
        "page_types": {
            "default_page_type": pt.get("default_page_type"),
            "default_lean": pt.get("default_lean"),
            "rules": pt.get("rules", []),
        },
    }


@router.get("/api/settings")
async def api_settings_get(site_id: str = Depends(get_site_id)) -> dict:
    return _settings_view(site_id)


def _clampi(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected a whole number")
    return max(lo, min(hi, n))


@router.post("/api/settings")
async def api_settings_post(body: dict = Body(...), site_id: str = Depends(get_site_id)) -> dict:
    """Persist editable settings as DB overrides and apply them immediately."""
    updates: dict[str, dict] = {}

    em = body.get("execution_mode")
    if em is not None:
        if em not in ("shadow", "live"):
            raise HTTPException(status_code=400, detail="execution_mode must be 'shadow' or 'live'")
        updates["runtime"] = {"execution_mode": em}

    g = body.get("guardrails") or {}
    if g:
        gov: dict = {}
        tz = g.get("timezone")
        if isinstance(tz, str) and tz.strip():
            gov["timezone"] = tz.strip()
        rl = {}
        if "max_outreach" in g:
            rl["max_outreach"] = _clampi(g["max_outreach"], 0, 50)
        if "window_days" in g:
            rl["window_days"] = _clampi(g["window_days"], 1, 365)
        if rl:
            gov["rate_limit"] = rl
        sw = {}
        if "send_start_hour" in g:
            sw["start_hour"] = _clampi(g["send_start_hour"], 0, 23)
        if "send_end_hour" in g:
            sw["end_hour"] = _clampi(g["send_end_hour"], 1, 24)
        if sw:
            gov["send_window"] = sw
        if gov:
            updates["guardrails"] = gov

    if not updates:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with transaction() as cur:
        for key, partial in updates.items():
            new_override = loader.merged_override(key, partial)
            await repo.upsert_site_config(cur, site_id, key, new_override)
            loader.set_override(key, new_override)

    return _settings_view(site_id)


@router.get("/api/map")
async def api_map(site_id: str = Depends(get_site_id)) -> dict:
    """Located visitors for the world map, each flagged live if seen very recently."""
    now = datetime.now(timezone.utc)
    live_cut = now - timedelta(seconds=LIVE_WINDOW_SECONDS)
    async with transaction() as cur:
        rows = await repo.list_map_visitors(cur, site_id)
    visitors, live = [], 0
    countries = set()
    for r in rows:
        is_live = r["last_seen_at"] is not None and r["last_seen_at"] > live_cut
        if is_live:
            live += 1
        if r["country"]:
            countries.add(r["country"])
        visitors.append({
            "lat": r["latitude"], "lng": r["longitude"],
            "city": r["city"], "region": r["region"], "country": r["country"],
            "funnel_stage": r["funnel_stage"], "intent_score": r["intent_score"],
            "name": r["name"], "email": r["email"],
            "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            "live": is_live,
        })
    return {
        "site_id": site_id,
        "total": len(visitors),
        "live": live,
        "countries": len(countries),
        "visitors": visitors,
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
