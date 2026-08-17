"""Layer 1 business logic: event ingest, page_type resolution (via config),
identity linking, and event backfill on identify.

Kept free of HTTP concerns (routers translate to/from these functions) and free
of SQL (repositories own that). Business rules for page typing live in config.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from config.loader import get_config, resolve_page_type
from db import repositories as repo
from db.connection import transaction
from tracking import geo

# anonymous_ids with an in-flight geo lookup, so repeated events don't re-hit the API
_geo_inflight: set[tuple[str, str]] = set()

ENGAGEMENT_EVENT = "page_engagement"
MAX_SECTIONS = 50            # per engagement ping — bounds a hostile/buggy client


def _sanitize_engagement(metadata: Optional[dict[str, Any]], site_id: str) -> dict[str, Any]:
    """Clamp a client-reported engagement delta.

    The browser is untrusted: active_ms and every section duration are clamped to
    the configured per-ping cap so no client can inflate its way to BOFU.
    """
    md = dict(metadata or {})
    cap_ms = int((get_config("stage_rules", site_id).get("engagement") or {})
                 .get("dwell_cap_seconds", 1800)) * 1000

    def _clamp_ms(value: Any) -> int:
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return 0
        return max(0, min(cap_ms, n))

    md["active_ms"] = _clamp_ms(md.get("active_ms"))
    md["elapsed_ms"] = _clamp_ms(md.get("elapsed_ms"))

    try:
        md["scroll_pct"] = max(0, min(100, int(float(md.get("scroll_pct", 0)))))
    except (TypeError, ValueError):
        md["scroll_pct"] = 0

    sections = md.get("sections")
    clean: dict[str, int] = {}
    if isinstance(sections, dict):
        for name, ms in list(sections.items())[:MAX_SECTIONS]:
            key = str(name).strip()[:60]
            value = _clamp_ms(ms)
            if key and value > 0:
                clean[key] = value
    md["sections"] = clean
    return md


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _resolve_geo(site_id: str, anonymous_id: str, ip: str) -> None:
    """Background: resolve IP -> approximate geo and store it (best-effort)."""
    key = (site_id, anonymous_id)
    try:
        loc = await geo.lookup_ip(ip)
        if not loc:
            return
        async with transaction() as cur:
            await repo.set_identity_geo(
                cur, site_id, anonymous_id,
                country=loc["country"], region=loc["region"], city=loc["city"],
                postal=loc.get("postal"), isp=loc.get("isp"),
                lat=loc.get("lat"), lng=loc.get("lng"),
            )
    except Exception:
        pass  # geo is optional; never surface ingestion errors
    finally:
        _geo_inflight.discard(key)


async def record_precise_location(
    *,
    site_id: str,
    anonymous_id: str,
    latitude: float,
    longitude: float,
    accuracy_m: Optional[float] = None,
) -> dict:
    """Store a consented browser GPS fix, then name it in the background.

    The coordinates are saved immediately (so nothing is lost if the reverse
    geocode fails or is slow) and the street/neighbourhood is filled in after.
    """
    accuracy = int(accuracy_m) if accuracy_m is not None else None

    async with transaction() as cur:
        await repo.get_or_create_identity(cur, site_id, anonymous_id)
        await repo.set_identity_precise_location(
            cur, site_id, anonymous_id,
            lat=latitude, lng=longitude, accuracy_m=accuracy,
        )

    asyncio.create_task(_name_location(site_id, anonymous_id, latitude, longitude, accuracy))
    return {"anonymous_id": anonymous_id, "accuracy_m": accuracy}


async def _name_location(site_id: str, anonymous_id: str, lat: float, lng: float,
                         accuracy_m: Optional[int]) -> None:
    """Background: reverse-geocode a GPS fix into street + neighbourhood."""
    try:
        place = await geo.reverse_geocode(lat, lng)
        if not place:
            return
        async with transaction() as cur:
            await repo.set_identity_precise_location(
                cur, site_id, anonymous_id,
                lat=lat, lng=lng, accuracy_m=accuracy_m,
                street=place.get("street"), district=place.get("district"),
                city=place.get("city"), region=place.get("region"),
                country=place.get("country"), postal=place.get("postal"),
            )
    except Exception:
        pass  # naming is a bonus; the coordinates are already stored


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    """Normalize an optional (possibly naive) timestamp to tz-aware UTC."""
    if dt is None:
        return _utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def track_event(
    *,
    site_id: str,
    event_type: str,
    url: Optional[str],
    timestamp: Optional[datetime],
    anonymous_id: str,
    session_id: Optional[str],
    metadata: Optional[dict[str, Any]],
    client_ip: Optional[str] = None,
    client_tz: Optional[str] = None,
) -> dict:
    """Ingest one event. If the anonymous_id is already identified, the event is
    attributed to that lead immediately (live attribution after identify)."""
    occurred_at = _ensure_utc(timestamp)
    page_type, _lean = resolve_page_type(url, site_id)
    is_heartbeat = event_type == "heartbeat"
    if event_type == ENGAGEMENT_EVENT:
        metadata = _sanitize_engagement(metadata, site_id)

    async with transaction() as cur:
        identity = await repo.get_or_create_identity(cur, site_id, anonymous_id)
        lead_id = identity.get("lead_id")

        # Browser timezone is an instant, no-lookup geo hint — set it once.
        if client_tz and not identity.get("timezone"):
            await repo.set_identity_timezone(cur, site_id, anonymous_id, client_tz)

        # Every ping refreshes "live now" presence (server time, so clock skew
        # on the client can't push a visitor out of the live window).
        await repo.upsert_presence(
            cur,
            site_id=site_id,
            anonymous_id=anonymous_id,
            session_id=session_id,
            url=url,
            page_type=page_type,
            last_seen=_utcnow(),
        )

        # Heartbeats are presence-only — they must NOT become events (that would
        # bloat history and inflate intent). Real events still get stored.
        event_id = None
        if not is_heartbeat:
            event = await repo.insert_event(
                cur,
                site_id=site_id,
                anonymous_id=anonymous_id,
                lead_id=lead_id,
                event_type=event_type,
                url=url,
                page_type=page_type,
                session_id=session_id,
                metadata=metadata,
                occurred_at=occurred_at,
            )
            event_id = event["id"]

    # Resolve IP -> country/region/city once per visitor, in the background so
    # /track stays fast. The raw IP is used here and never persisted.
    if client_ip and not identity.get("country"):
        key = (site_id, anonymous_id)
        if key not in _geo_inflight:
            _geo_inflight.add(key)
            asyncio.create_task(_resolve_geo(site_id, anonymous_id, client_ip))

    return {
        "event_id": event_id,
        "lead_id": lead_id,
        "page_type": page_type,
    }


async def identify(
    *,
    site_id: str,
    anonymous_id: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    email_opt_in: Optional[bool],
    whatsapp_opt_in: Optional[bool],
    consent_timestamp: Optional[datetime],
    consent_source: Optional[str],
    name: Optional[str] = None,
    phone_verified: bool = False,
) -> dict:
    """Resolve a person to a lead, refresh consent, backfill their events.

    Resolution precedence keeps one lead per person while honoring an anonymous
    profile that scoring may already have created for this visitor:
      1. An existing identified lead matched by email/phone wins.
      2. Else the anonymous_id's current profile lead (if any) is enriched.
      3. Else a new lead is created.
    A stale anonymous profile displaced by case 1 is deleted if unused. Atomic.

    anonymous_id is OPTIONAL. It is what ties the person to the browsing they did
    before identifying themselves, but it comes from a cookie — blocked trackers,
    private windows and second devices all arrive without one. The lead is still
    recorded in that case; only the history linkage is skipped, because contact
    details for someone who just proved their phone number are worth far more
    than the pages they happened to read.
    """
    consent_ts = _ensure_utc(consent_timestamp)

    async with transaction() as cur:
        current_lead_id = None
        if anonymous_id:
            identity = await repo.get_or_create_identity(cur, site_id, anonymous_id)
            current_lead_id = identity.get("lead_id")

        match = None
        if email:
            match = await repo.find_lead_by_email(cur, site_id, email)
        if match is None and phone:
            match = await repo.find_lead_by_phone(cur, site_id, phone)

        created = False
        if match is not None:
            lead = await repo.update_lead_consent(
                cur,
                lead_id=match["id"],
                email=email,
                phone=phone,
                email_opt_in=email_opt_in,
                whatsapp_opt_in=whatsapp_opt_in,
                consent_timestamp=consent_ts,
                consent_source=consent_source,
                name=name,
                phone_verified=phone_verified,
            )
            if current_lead_id and current_lead_id != lead["id"]:
                await repo.delete_lead_if_anonymous_orphan(
                    cur, site_id, current_lead_id, keep_anonymous_id=anonymous_id
                )
        elif current_lead_id is not None:
            lead = await repo.update_lead_consent(
                cur,
                lead_id=current_lead_id,
                email=email,
                phone=phone,
                email_opt_in=email_opt_in,
                whatsapp_opt_in=whatsapp_opt_in,
                consent_timestamp=consent_ts,
                consent_source=consent_source,
                name=name,
                phone_verified=phone_verified,
            )
        else:
            created = True
            lead = await repo.create_lead(
                cur,
                site_id=site_id,
                email=email,
                phone=phone,
                # A brand-new lead has no prior consent to preserve, so an
                # unstated flag is simply "no".
                email_opt_in=bool(email_opt_in),
                whatsapp_opt_in=bool(whatsapp_opt_in),
                consent_timestamp=consent_ts,
                consent_source=consent_source,
                name=name,
                phone_verified=phone_verified,
            )

        backfilled = 0
        if anonymous_id:
            await repo.link_identity(cur, site_id, anonymous_id, lead["id"])
            backfilled = await repo.backfill_events_to_lead(cur, site_id, anonymous_id, lead["id"])

    return {
        "lead_id": lead["id"],
        "created": created,
        "anonymous_id": anonymous_id,
        "backfilled_events": backfilled,
        "phone_verified": bool(lead.get("phone_verified")),
    }
