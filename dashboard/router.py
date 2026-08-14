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
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from config import loader
from config.loader import effective_execution_mode, get_config
from config.settings import get_settings
from db import repositories as repo
from db.connection import transaction
from deps import get_site_id
from scoring import engagement

router = APIRouter(tags=["dashboard"])
_TPL_DIR = pathlib.Path(__file__).resolve().parent / "templates"
TEMPLATE = _TPL_DIR / "dashboard.html"
INSIGHTS_TEMPLATE = _TPL_DIR / "insights.html"
MAP_TEMPLATE = _TPL_DIR / "map.html"
SETTINGS_TEMPLATE = _TPL_DIR / "settings.html"

LIVE_WINDOW_SECONDS = 24   # "live" if seen within this window (≈2.4× the 10s heartbeat, so no flicker)
INSIGHTS_DAYS = 14         # trend window for the insights page
DWELL_CAP_SECONDS = 1800   # cap a single page's counted dwell so idle time isn't over-counted


def _iso(dt):
    """Serialize a timestamp as UTC with an explicit offset.

    Postgres hands back TIMESTAMPTZ values in the *session's* timezone, so the
    offset in the raw isoformat() varies by host (UTC on Railway, local on a dev
    machine). Normalizing here means the browser always receives an unambiguous
    instant and can render it in the viewer's own timezone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:                     # naive values are UTC by convention here
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _path(url):
    if not url:
        return "/"
    try:
        p = urlparse(url)
        return (p.path or "/") + (("?" + p.query) if p.query else "")
    except Exception:
        return url


def _bare_path(url):
    """Path with the query string and fragment dropped — matches how SQL groups
    pages, which matters when building visit keys that must line up with the
    engine's. urlparse already splits the fragment off `path`."""
    if not url:
        return "/"
    try:
        return urlparse(url).path or "/"
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


def _reporting_timezone(requested: Optional[str], site_id: str) -> str:
    """Resolve the timezone that day boundaries are measured in.

    The viewer's own zone wins — "today" should mean today where they are — with
    the site's configured zone as the fallback. An unknown name silently degrades
    to UTC rather than erroring the whole page.
    """
    candidates = [requested, get_config("guardrails", site_id).get("timezone")]
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return "UTC"


def _empty_day(day: str) -> dict:
    return {"day": day, "visitors": 0, "new_visitors": 0, "returning_visitors": 0,
            "multi_session_visitors": 0, "sessions": 0, "pageviews": 0}


@router.get("/api/insights")
async def api_insights(tz: Optional[str] = None, site_id: str = Depends(get_site_id)) -> dict:
    """Page analytics: totals, per-page views, page-type split, and a daily trend."""
    zone = _reporting_timezone(tz, site_id)

    async with transaction() as cur:
        totals = await repo.insights_totals(cur, site_id)
        pages = await repo.page_views_breakdown(cur, site_id)
        page_types = await repo.page_type_breakdown(cur, site_id)
        by_day_rows = await repo.views_by_day(cur, site_id, INSIGHTS_DAYS)
        visitor_rows = await repo.visitor_daily_breakdown(cur, site_id, zone, INSIGHTS_DAYS)

    # fill missing days so the trend line is continuous
    today = datetime.now(timezone.utc).date()
    seen = {r["day"]: r for r in by_day_rows}
    by_day = []
    for i in range(INSIGHTS_DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = seen.get(d)
        by_day.append({"day": d, "views": (row["views"] if row else 0),
                       "visitors": (row["visitors"] if row else 0)})

    # Today/yesterday in the reporting zone, so a day with no traffic reads as
    # zero rather than going missing from the response.
    local_today = datetime.now(ZoneInfo(zone)).date()
    seen_visitors = {r["day"]: r for r in visitor_rows}
    today_key = local_today.isoformat()
    yesterday_key = (local_today - timedelta(days=1)).isoformat()

    return {
        "site_id": site_id,
        "totals": totals,
        "pages": pages,
        "page_types": page_types,
        "by_day": by_day,
        "visitors": {
            "timezone": zone,
            "today": seen_visitors.get(today_key) or _empty_day(today_key),
            "yesterday": seen_visitors.get(yesterday_key) or _empty_day(yesterday_key),
            "series": visitor_rows,
        },
    }


def classify_lead(lead: dict, floor_seconds: float) -> tuple[str, Optional[str]]:
    """Split leads into 'real' and 'junk', with the reason.

    Junk means there is nothing to work with AND nothing to learn from: the
    visitor never said who they were, never clicked anything, saw a single page,
    and did not stay on it long enough to count. Every condition must hold, so
    the label errs toward keeping a lead.

    Anyone who identified themselves is real regardless of how briefly they
    browsed — a phone number is the whole point. And a lead with no engagement
    telemetry but several page views stays real too, because absence of
    measurement is not evidence of a bounce (leads recorded before dwell
    tracking existed would otherwise all be condemned).
    """
    if lead.get("email") or lead.get("phone"):
        return "real", None
    if (lead.get("clicks") or 0) > 0:
        return "real", None
    if (lead.get("pageviews") or 0) > 1:
        return "real", None
    if float(lead.get("active_seconds") or 0) >= floor_seconds:
        return "real", None
    return "junk", "one page, no clicks, under the minimum dwell, never identified"


@router.get("/api/leads")
async def api_leads(site_id: str = Depends(get_site_id)) -> dict:
    floor = float((get_config("stage_rules", site_id).get("engagement") or {})
                  .get("min_seconds_per_view", 8))
    async with transaction() as cur:
        leads = await repo.list_leads(cur, site_id)

    counts = {"real": 0, "junk": 0}
    for lead in leads:
        quality, reason = classify_lead(lead, floor)
        lead["quality"] = quality
        lead["junk_reason"] = reason
        # Decimal from SQL is not JSON-serializable by default.
        lead["active_seconds"] = round(float(lead.get("active_seconds") or 0), 1)
        counts[quality] += 1

    return {
        "site_id": site_id,
        "leads": leads,
        "counts": {**counts, "all": len(leads), "min_seconds_per_view": floor},
    }


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
        behavior = await repo.lead_behavior_rows(cur, site_id, lead_id)

    eng = engagement.build(behavior, site_id)
    default_page_type = get_config("page_types", site_id).get("default_page_type") or "other"
    dwell_cap = int((get_config("stage_rules", site_id).get("engagement") or {})
                    .get("dwell_cap_seconds", DWELL_CAP_SECONDS))
    # Measured active seconds per visit, so the timeline shows real attention
    # rather than the gap between two timestamps.
    measured_by_vid: dict[str, float] = {}
    sections_by_vid: dict[str, dict[str, float]] = {}
    for row in behavior:
        if row["event_type"] != "page_engagement":
            continue
        vid = engagement.visit_key(row.get("vid"), row.get("session_id"), row.get("path"))
        md = row.get("metadata") or {}
        measured_by_vid[vid] = measured_by_vid.get(vid, 0.0) + float(md.get("active_ms") or 0) / 1000.0
        bucket = sections_by_vid.setdefault(vid, {})
        for name, ms in (md.get("sections") or {}).items():
            try:
                bucket[name] = bucket.get(name, 0.0) + float(ms) / 1000.0
            except (TypeError, ValueError):
                continue

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
        elapsed = max(0.0, (end - start).total_seconds())
        metadata = e["metadata"] or {}
        # Same key and same thresholds the scoring engine used, so what the
        # timeline shows can never disagree with the stage it explains.
        vid = engagement.visit_key(metadata.get("vid"), e["session_id"], _bare_path(e["url"]))
        is_view = e["event_type"] == "page_view"
        measured = measured_by_vid.get(vid)
        page_type = e["page_type"] or default_page_type
        floor = eng["thresholds"].get(page_type, eng["thresholds"].get(default_page_type, 0))
        active = min(measured, dwell_cap) if measured is not None else None
        timeline.append({
            "event_type": e["event_type"],
            "url": e["url"],
            "path": _path(e["url"]),
            "page_type": page_type,
            "session_id": e["session_id"],
            "metadata": metadata,
            "occurred_at": _iso(e["occurred_at"]),
            # elapsed = wall clock to the next event; active = measured attention.
            "dwell_seconds": round(elapsed) if is_view else None,
            "active_seconds": round(active) if (is_view and active is not None) else None,
            "min_seconds": floor if is_view else None,
            "qualified": (active is not None and active >= floor) if is_view else None,
            "sections": {k: round(v) for k, v in sorted(
                sections_by_vid.get(vid, {}).items(), key=lambda kv: kv[1], reverse=True)
            } if is_view else None,
        })

    pageviews = [e for e in events if e["event_type"] == "page_view"]
    clicks = [e for e in events if e["event_type"] != "page_view"]
    total_time = max(0.0, (last_activity - first).total_seconds()) if (first and last_activity) else 0
    active_time = eng["active_seconds"]
    page_counts = Counter(_path(e["url"]) for e in pageviews)

    return {
        "site_id": site_id,
        "lead_id": lead_id,
        "summary": {
            "funnel_stage": lead.get("funnel_stage"),
            "intent_score": lead.get("intent_score"),
            # Why this lead sits where it does — the rule that fired, or the
            # model plus any gate that downgraded it.
            "stage_source": lead.get("stage_source"),
            "stage_reason": lead.get("stage_reason"),
            "name": lead.get("name"),
            "email": lead.get("email"),
            "country": location.get("country"),
            "region": location.get("region"),
            "city": location.get("city"),
            # Street/district exist only for a consented GPS fix; location_source
            # and accuracy_m let the UI show how much to trust any of it.
            "district": location.get("district"),
            "street": location.get("street"),
            "postal": location.get("postal"),
            "isp": location.get("isp"),
            "accuracy_m": location.get("accuracy_m"),
            "location_source": location.get("location_source"),
            "timezone": location.get("timezone"),
            "events": n,
            "pageviews": len(pageviews),
            "clicks": len(clicks),
            "sessions": len({e["session_id"] for e in events if e["session_id"]}),
            "pages_distinct": len(page_counts),
            "first_seen": _iso(first),
            "last_seen": _iso(last_activity),
            "total_time_seconds": round(total_time),
            "active_time_seconds": round(active_time),
            "qualified_pages": eng["qualified_visits"],
            "top_pages": page_counts.most_common(8),
        },
        "engagement": {
            "by_page_type": eng["by_page_type"],
            "by_path": eng["by_path"],
            "by_lean": eng["by_lean"],
            "sections": eng["sections"],
            "thresholds": eng["thresholds"],
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
    sr = get_config("stage_rules", site_id)
    sc = get_config("scoring", site_id)
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
        "stage_rules": {
            "enabled": bool(sr.get("enabled", True)),
            "mode": sr.get("mode", "rules_first"),
            "engagement": sr.get("engagement", {}) or {},
            "rules": sr.get("rules", []) or [],
            "gates": sr.get("gates", {}) or {},
        },
        "stages": sc.get("funnel_stages", ["TOFU", "MOFU", "BOFU"]),
    }


@router.get("/api/settings")
async def api_settings_get(site_id: str = Depends(get_site_id)) -> dict:
    view = _settings_view(site_id)
    async with transaction() as cur:
        view["vocabulary"] = await repo.observed_vocabulary(cur, site_id)
    return view


def _clampi(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected a whole number")
    return max(lo, min(hi, n))


def _optional_clampi(v, lo, hi):
    """None/'' means 'not set' — the key is dropped rather than defaulted to 0."""
    if v is None or v == "":
        return None
    return _clampi(v, lo, hi)


# Which numeric keys each condition type accepts, so a hand-edited or malformed
# rule can never reach the engine. Anything not listed here is dropped.
_COND_LIMITS = {
    "min_seconds": (0, 86400), "max_seconds": (0, 86400),
    "min_views": (0, 1000), "max_views": (0, 1000),
    "min_raw_views": (0, 1000), "max_raw_views": (0, 1000),
    "min_clicks": (0, 1000), "max_clicks": (0, 1000),
    "min_scroll_pct": (0, 100), "max_scroll_pct": (0, 100),
    "min_count": (0, 1000), "max_count": (0, 1000),
    "min_active_seconds": (0, 86400), "max_active_seconds": (0, 86400),
    "min_pageviews": (0, 1000), "max_pageviews": (0, 1000),
    "min_raw_pageviews": (0, 1000), "max_raw_pageviews": (0, 1000),
    "min_sessions": (0, 1000), "max_sessions": (0, 1000),
    "min_active_days": (0, 365), "max_active_days": (0, 365),
    "min_distinct_pages": (0, 1000), "max_distinct_pages": (0, 1000),
    "min_measured_visits": (0, 1000), "max_measured_visits": (0, 1000),
}

_COND_FIELDS = {
    "page_type": "page_type",
    "path": "contains",
    "section": "name",
    "event": "event_type",
    "total": None,
}

# Metrics each condition type can actually test. Keeping this tight matters:
# a metric the engine doesn't compute for that type evaluates to False forever,
# so a rule silently stops matching. Stale keys (e.g. left over from switching a
# condition's type) are dropped here rather than persisted.
_COND_METRICS = {
    "page_type": {"seconds", "views", "raw_views", "clicks", "scroll_pct"},
    "path": {"seconds", "views", "raw_views", "clicks", "scroll_pct"},
    "section": {"seconds", "views"},
    "event": {"count"},
    "total": {"active_seconds", "pageviews", "raw_pageviews", "sessions",
              "active_days", "distinct_pages", "clicks", "measured_visits"},
}


def _clean_condition(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    ctype = str(raw.get("type") or "total")
    if ctype not in _COND_FIELDS:
        return None
    cond: dict = {"type": ctype}

    field = _COND_FIELDS[ctype]
    if field:
        value = str(raw.get(field) or "").strip()[:120]
        if value:
            cond[field] = value
        elif ctype != "event":
            return None            # a page/path/section condition needs a target

    if ctype == "event":
        texts = raw.get("text_contains")
        if isinstance(texts, str):
            texts = [t for t in texts.split(",")]
        if isinstance(texts, list):
            cleaned = [str(t).strip().lower()[:80] for t in texts if str(t).strip()][:20]
            if cleaned:
                cond["text_contains"] = cleaned
        if not cond.get("event_type") and not cond.get("text_contains"):
            return None

    allowed = _COND_METRICS[ctype]
    for key, (lo, hi) in _COND_LIMITS.items():
        if key[4:] not in allowed:
            continue
        if key in raw and raw[key] not in (None, ""):
            cond[key] = _clampi(raw[key], lo, hi)

    if "identified" in raw and raw["identified"] not in (None, ""):
        value = raw["identified"]
        # bool("false") is True — coerce the strings a non-browser caller may send.
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "known")
        cond["identified"] = bool(value)

    # A condition with a target but no threshold would match anything.
    if not any(k.startswith(("min_", "max_")) for k in cond) and "identified" not in cond:
        return None
    return cond


def _clean_stage_rules(body: dict, stages: list[str]) -> dict:
    """Validate the whole stage-rules block coming from the Settings page."""
    out: dict = {}

    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    mode = body.get("mode")
    if mode is not None:
        if mode not in ("rules_first", "rules_only", "llm_only"):
            raise HTTPException(status_code=400, detail="mode must be rules_first, rules_only or llm_only")
        out["mode"] = mode

    eng = body.get("engagement")
    if isinstance(eng, dict):
        clean_eng = {}
        if "min_seconds_per_view" in eng:
            clean_eng["min_seconds_per_view"] = _clampi(eng["min_seconds_per_view"], 0, 3600)
        if "dwell_cap_seconds" in eng:
            clean_eng["dwell_cap_seconds"] = _clampi(eng["dwell_cap_seconds"], 30, 86400)
        if clean_eng:
            out["engagement"] = clean_eng

    if isinstance(body.get("rules"), list):
        rules_out = []
        for raw in body["rules"][:100]:
            if not isinstance(raw, dict):
                continue
            stage = raw.get("stage")
            if stage not in stages:
                raise HTTPException(status_code=400, detail=f"rule stage must be one of {stages}")
            when_in = raw.get("when") or {}
            when: dict = {}
            for group in ("all", "any", "none"):
                conds = [c for c in (_clean_condition(c) for c in (when_in.get(group) or [])) if c]
                if conds:
                    when[group] = conds
            if not when:
                raise HTTPException(
                    status_code=400,
                    detail=f"rule '{raw.get('name') or 'unnamed'}' needs at least one complete condition "
                           "(a target plus a threshold)",
                )
            rule = {
                "name": str(raw.get("name") or "Unnamed rule").strip()[:80],
                "stage": stage,
                "enabled": bool(raw.get("enabled", True)),
                "when": when,
            }
            score = _optional_clampi(raw.get("intent_score"), 0, 100)
            if score is not None:
                rule["intent_score"] = score
            rules_out.append(rule)
        # Rules are ORDER-SENSITIVE (first match wins), so this list replaces the
        # old one wholesale instead of deep-merging into it.
        out["rules"] = rules_out

    if isinstance(body.get("gates"), dict):
        gates_out: dict = {}
        for stage, gate in body["gates"].items():
            if stage not in stages or not isinstance(gate, dict):
                continue
            clean: dict = {}
            for key in ("min_active_seconds", "min_pageviews", "min_sessions",
                        "min_distinct_pages", "min_active_days"):
                value = _optional_clampi(gate.get(key), 0, 86400)
                if value is not None:
                    clean[key] = value
            # Per-lean minimums may name ANY stage, not just this gate's own —
            # "BOFU requires 40s on BOFU pages and 60s on MOFU pages" is valid.
            for key in ("min_lean_seconds", "min_lean_views"):
                per_lean = gate.get(key)
                if not isinstance(per_lean, dict):
                    continue
                cleaned = {k: _clampi(v, 0, 86400) for k, v in per_lean.items()
                           if k in stages and v not in (None, "")}
                if cleaned:
                    clean[key] = cleaned
            gates_out[stage] = clean
        out["gates"] = gates_out

    return out


def _clean_page_types(body: dict) -> dict:
    """Validate the page-type rules coming from the Settings page."""
    out: dict = {}
    if body.get("default_page_type"):
        out["default_page_type"] = str(body["default_page_type"]).strip()[:40]
    if body.get("default_lean"):
        out["default_lean"] = str(body["default_lean"]).strip()[:10]

    if isinstance(body.get("rules"), list):
        rules_out = []
        for raw in body["rules"][:200]:
            if not isinstance(raw, dict):
                continue
            page_type = str(raw.get("page_type") or "").strip()[:40]
            tokens = raw.get("match_any")
            if isinstance(tokens, str):
                tokens = tokens.split(",")
            tokens = [str(t).strip().lower()[:120] for t in (tokens or []) if str(t).strip()][:40]
            if not page_type or not tokens:
                continue
            rule = {"page_type": page_type, "match_any": tokens}
            lean = str(raw.get("lean") or "").strip()[:10]
            if lean:
                rule["lean"] = lean
            secs = _optional_clampi(raw.get("min_seconds"), 0, 3600)
            if secs is not None:
                rule["min_seconds"] = secs
            rules_out.append(rule)
        if not rules_out:
            raise HTTPException(status_code=400, detail="at least one page-type rule is required")
        # First match wins here too — replace, don't merge.
        out["rules"] = rules_out
    return out


@router.post("/api/settings")
async def api_settings_post(body: dict = Body(...), site_id: str = Depends(get_site_id)) -> dict:
    """Persist editable settings as DB overrides and apply them immediately."""
    updates: dict[str, dict] = {}
    # Blocks whose override REPLACES the YAML file wholesale (see loader). Rules
    # are ordered lists — merging them would resurrect rules the user deleted.
    replaced: set[str] = set()

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

    stages = get_config("scoring", site_id).get("funnel_stages", ["TOFU", "MOFU", "BOFU"])

    pt = body.get("page_types")
    if isinstance(pt, dict) and pt:
        # Start from the effective config so a partial POST can't blank the block.
        updates["page_types"] = {**get_config("page_types", site_id), **_clean_page_types(pt)}
        replaced.add("page_types")

    sr = body.get("stage_rules")
    if isinstance(sr, dict) and sr:
        updates["stage_rules"] = {**get_config("stage_rules", site_id),
                                  **_clean_stage_rules(sr, stages)}
        replaced.add("stage_rules")

    if not updates:
        raise HTTPException(status_code=400, detail="nothing to update")

    async with transaction() as cur:
        for key, partial in updates.items():
            new_override = partial if key in replaced else loader.merged_override(key, partial)
            await repo.upsert_site_config(cur, site_id, key, new_override)
            loader.set_override(key, new_override)

    view = _settings_view(site_id)
    async with transaction() as cur:
        view["vocabulary"] = await repo.observed_vocabulary(cur, site_id)
    return view


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
            "district": r["district"], "street": r["street"], "postal": r["postal"],
            "isp": r["isp"], "accuracy_m": r["accuracy_m"],
            "location_source": r["location_source"],
            "funnel_stage": r["funnel_stage"], "intent_score": r["intent_score"],
            "name": r["name"], "email": r["email"],
            "last_seen_at": _iso(r["last_seen_at"]),
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
