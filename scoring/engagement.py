"""Layer 2, Stage A — engagement rollup.

Turns the raw event stream into the measurements the rule engine reasons about:

  * per VISIT (one page view): active seconds, clicks, scroll depth, sections read
  * per page type / path: qualified views, seconds, clicks
  * per section: seconds

The key idea is qualification. A visit only counts once its active time clears
the page type's `min_seconds` (config/page_types.yaml). Every TIME measurement
downstream — seconds per page type, per lean, per section, and the journey total —
sums qualified visits only, so neither one glance at /pricing nor twenty of them
add up to buying intent. Raw equivalents are kept alongside for contrast.

Clicks are the exception: an explicit action counts whether or not the visit that
contained it was long enough, because clicking "Book a call" and leaving is intent,
not a bounce.

Active seconds come from the browser's `page_engagement` pings, which count only
tab-visible, non-idle time; clock-based fallbacks are never used, because a tab
left open overnight would otherwise look like the most engaged lead on the site.
"""
from __future__ import annotations

from typing import Any, Optional

from config.loader import get_config, min_seconds_by_page_type, page_type_leans

ENGAGEMENT_EVENT = "page_engagement"
PAGE_VIEW_EVENT = "page_view"
NON_CLICK_EVENTS = {PAGE_VIEW_EVENT, ENGAGEMENT_EVENT, "heartbeat"}


def visit_key(vid: Optional[str], session_id: Optional[str], path: Optional[str]) -> str:
    """Group events into one visit. `vid` is exact; events recorded before the
    snippet emitted visit ids fall back to (session, path).

    Callers outside this module must pass the same query-stripped path the SQL
    layer produces, or their fallback keys won't line up with these.
    """
    if vid:
        return str(vid)
    return f"{session_id or '-'}|{path or '/'}"


def _visit_key(row: dict) -> str:
    return visit_key(row.get("vid"), row.get("session_id"), row.get("path"))


def _click_label(metadata: Optional[dict]) -> str:
    md = metadata or {}
    parts = [md.get("text"), md.get("id"), md.get("href")]
    return " ".join(str(p) for p in parts if p).lower()


def build(rows: list[dict], site_id: str = "default") -> dict[str, Any]:
    """Roll a lead's event rows up into the engagement summary."""
    thresholds = min_seconds_by_page_type(site_id)
    leans = page_type_leans(site_id)
    # Ingest clamps each ping; this clamps the visit, so stacking pings can't
    # inflate one page view past the cap either.
    visit_cap = float((get_config("stage_rules", site_id).get("engagement") or {})
                      .get("dwell_cap_seconds", 1800))

    default_page_type = get_config("page_types", site_id).get("default_page_type") or "other"

    visits: dict[str, dict[str, Any]] = {}
    sections_total: dict[str, float] = {}
    section_visits: dict[str, int] = {}
    events_by_type: dict[str, int] = {}
    click_labels: list[str] = []
    labels_by_type: dict[str, list[str]] = {}

    for row in rows:
        event_type = row.get("event_type") or ""
        key = _visit_key(row)
        visit = visits.get(key)
        if visit is None:
            visit = visits[key] = {
                "path": row.get("path") or "/",
                "page_type": row.get("page_type"),
                "session_id": row.get("session_id"),
                "started_at": row.get("occurred_at"),
                "views": 0,
                "clicks": 0,
                "seconds": 0.0,
                "scroll_pct": 0,
                "sections": {},
            }
        # A page view names the visit; engagement pings may arrive first on SPAs.
        if event_type == PAGE_VIEW_EVENT:
            visit["views"] += 1
            visit["page_type"] = row.get("page_type")
            visit["path"] = row.get("path") or visit["path"]

        if event_type == ENGAGEMENT_EVENT:
            md = row.get("metadata") or {}
            visit["seconds"] += float(md.get("active_ms") or 0) / 1000.0
            try:
                visit["scroll_pct"] = max(int(visit["scroll_pct"]), int(md.get("scroll_pct") or 0))
            except (TypeError, ValueError):
                pass
            for name, ms in (md.get("sections") or {}).items():
                try:
                    secs = float(ms) / 1000.0
                except (TypeError, ValueError):
                    continue
                visit["sections"][name] = visit["sections"].get(name, 0.0) + secs
        elif event_type not in NON_CLICK_EVENTS:
            visit["clicks"] += 1
            events_by_type[event_type] = events_by_type.get(event_type, 0) + 1
            label = _click_label(row.get("metadata"))
            if label:
                click_labels.append(label)
                # Kept per event type too, so a rule can say "a *click* labelled
                # 'book a call'" and not match a form_submit with the same text.
                labels_by_type.setdefault(event_type, []).append(label)

    # --- qualification -------------------------------------------------------
    by_page_type: dict[str, dict[str, float]] = {}
    by_path: dict[str, dict[str, float]] = {}
    by_lean: dict[str, dict[str, float]] = {}
    qualified_visits = 0
    total_seconds = 0.0

    raw_seconds = 0.0
    for visit in visits.values():
        # Cap the visit as a whole: ingest already clamps each ping, this stops a
        # client stacking pings to inflate one page view past the cap.
        visit["seconds"] = round(min(visit["seconds"], visit_cap), 1)
        # Section time can't exceed the visit that contained it.
        visit["sections"] = {k: min(v, visit["seconds"]) for k, v in visit["sections"].items()}

        page_type = visit["page_type"]
        floor = thresholds.get(page_type, thresholds.get(default_page_type, 0))
        visit["qualified"] = visit["seconds"] >= floor
        visit["min_seconds"] = floor
        raw_seconds += visit["seconds"]
        if visit["qualified"]:
            total_seconds += visit["seconds"]

        for bucket, name in ((by_page_type, page_type or default_page_type), (by_path, visit["path"])):
            slot = bucket.setdefault(name, {"views": 0, "qualified_views": 0, "seconds": 0.0,
                                            "raw_seconds": 0.0, "clicks": 0, "max_scroll_pct": 0})
            slot["views"] += max(1, visit["views"])
            slot["raw_seconds"] += visit["seconds"]
            slot["clicks"] += visit["clicks"]          # actions count either way
            if visit["qualified"]:
                slot["qualified_views"] += 1
                slot["seconds"] += visit["seconds"]
                slot["max_scroll_pct"] = max(slot["max_scroll_pct"], visit["scroll_pct"])

        if not visit["qualified"]:
            continue

        qualified_visits += 1
        for name, secs in visit["sections"].items():
            sections_total[name] = sections_total.get(name, 0.0) + secs
            section_visits[name] = section_visits.get(name, 0) + 1

        lean = leans.get(page_type)
        if lean:
            slot = by_lean.setdefault(lean, {"views": 0, "seconds": 0.0, "clicks": 0})
            slot["views"] += 1
            slot["seconds"] += visit["seconds"]
            slot["clicks"] += visit["clicks"]

    for bucket in (by_page_type, by_path, by_lean):
        for slot in bucket.values():
            slot["seconds"] = round(slot["seconds"], 1)
            if "raw_seconds" in slot:
                slot["raw_seconds"] = round(slot["raw_seconds"], 1)

    return {
        "by_page_type": by_page_type,
        "by_path": by_path,
        "by_lean": by_lean,
        "sections": {k: round(v, 1) for k, v in sorted(
            sections_total.items(), key=lambda kv: kv[1], reverse=True)},
        "section_visits": section_visits,
        "events_by_type": events_by_type,
        "click_labels": click_labels,
        "labels_by_type": labels_by_type,
        "active_seconds": round(total_seconds, 1),
        "raw_active_seconds": round(raw_seconds, 1),
        "visits": len(visits),
        "qualified_visits": qualified_visits,
        # Visits that actually reported time. Zero means this lead predates
        # engagement tracking (or the browser never flushed) — rules that judge a
        # visitor by how SHORT their time was must not fire on absent data.
        "measured_visits": sum(1 for v in visits.values() if v["seconds"] > 0),
        "distinct_pages": len({v["path"] for v in visits.values() if v["qualified"]}),
        "sessions": len({v["session_id"] for v in visits.values()
                         if v["session_id"] and v["qualified"]}),
        "clicks": sum(v["clicks"] for v in visits.values()),
        "thresholds": thresholds,
    }
