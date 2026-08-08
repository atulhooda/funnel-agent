"""Data-access functions per table.

All reads/writes are site_id-scoped. This is the ONLY place that speaks SQL;
layers call these functions with a cursor obtained from db.connection.transaction.
Layer 1 uses the identities / events / leads helpers below; later layers extend
this module (decisions, sent_messages, scoring reads).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from psycopg.types.json import Jsonb

# --------------------------------------------------------------------------- #
# identities
# --------------------------------------------------------------------------- #

async def get_or_create_identity(cur, site_id: str, anonymous_id: str) -> dict:
    """Return the identity row for (site_id, anonymous_id), creating it if new.

    The no-op DO UPDATE makes RETURNING yield the existing row on conflict, so
    a known anonymous_id brings back its current lead_id.
    """
    await cur.execute(
        """
        INSERT INTO identities (site_id, anonymous_id)
        VALUES (%s, %s)
        ON CONFLICT (site_id, anonymous_id)
        DO UPDATE SET anonymous_id = EXCLUDED.anonymous_id
        RETURNING id, site_id, anonymous_id, lead_id, country, region, city, timezone
        """,
        (site_id, anonymous_id),
    )
    return await cur.fetchone()


async def set_identity_geo(
    cur, site_id: str, anonymous_id: str, *,
    country, region, city, lat=None, lng=None, postal=None, isp=None,
) -> None:
    """Store approximate IP geo.

    Only fills columns that are still empty, and skips the row entirely once a
    GPS fix exists — a precise location must never be overwritten by an IP guess.
    """
    await cur.execute(
        """
        UPDATE identities
           SET country         = COALESCE(country, %s),
               region          = COALESCE(region, %s),
               city            = COALESCE(city, %s),
               postal          = COALESCE(postal, %s),
               isp             = COALESCE(isp, %s),
               latitude        = COALESCE(latitude, %s),
               longitude       = COALESCE(longitude, %s),
               location_source = COALESCE(location_source, 'ip'),
               located_at      = COALESCE(located_at, now())
         WHERE site_id = %s AND anonymous_id = %s
           AND COALESCE(location_source, 'ip') <> 'gps'
        """,
        (country, region, city, postal, isp, lat, lng, site_id, anonymous_id),
    )


async def set_identity_precise_location(
    cur, site_id: str, anonymous_id: str, *,
    lat: float, lng: float, accuracy_m=None,
    street=None, district=None, city=None, region=None, country=None, postal=None,
) -> None:
    """Store a consented browser GPS fix — this OVERWRITES the IP estimate.

    Reverse-geocoded names are only written when present, so a failed lookup
    still keeps the coordinates (and whatever the IP had already given us).
    """
    await cur.execute(
        """
        UPDATE identities
           SET latitude        = %s,
               longitude       = %s,
               accuracy_m      = %s,
               street          = COALESCE(%s, street),
               district        = COALESCE(%s, district),
               city            = COALESCE(%s, city),
               region          = COALESCE(%s, region),
               country         = COALESCE(%s, country),
               postal          = COALESCE(%s, postal),
               location_source = 'gps',
               located_at      = now()
         WHERE site_id = %s AND anonymous_id = %s
        """,
        (lat, lng, accuracy_m, street, district, city, region, country, postal,
         site_id, anonymous_id),
    )


async def set_identity_timezone(cur, site_id: str, anonymous_id: str, timezone: str) -> None:
    await cur.execute(
        "UPDATE identities SET timezone = COALESCE(timezone, %s) WHERE site_id = %s AND anonymous_id = %s",
        (timezone, site_id, anonymous_id),
    )


_EMPTY_LOCATION = {
    "country": None, "region": None, "city": None, "district": None, "street": None,
    "postal": None, "isp": None, "timezone": None, "accuracy_m": None,
    "location_source": None, "latitude": None, "longitude": None,
}


async def lead_location(cur, site_id: str, lead_id: int) -> dict:
    """Best available location for a lead.

    A GPS fix wins over an IP estimate regardless of which identity is newer —
    a consented fix from last week beats an IP guess from this morning.
    """
    await cur.execute(
        """
        SELECT country, region, city, district, street, postal, isp, timezone,
               accuracy_m, location_source, latitude, longitude
        FROM identities
        WHERE site_id = %s AND lead_id = %s
          AND (country IS NOT NULL OR city IS NOT NULL OR timezone IS NOT NULL)
        ORDER BY (location_source = 'gps') DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (site_id, lead_id),
    )
    return (await cur.fetchone()) or dict(_EMPTY_LOCATION)


async def link_identity(cur, site_id: str, anonymous_id: str, lead_id: int) -> dict:
    """Attach an anonymous_id to a lead (create the mapping if it did not exist)."""
    await cur.execute(
        """
        INSERT INTO identities (site_id, anonymous_id, lead_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (site_id, anonymous_id)
        DO UPDATE SET lead_id = EXCLUDED.lead_id
        RETURNING id, site_id, anonymous_id, lead_id
        """,
        (site_id, anonymous_id, lead_id),
    )
    return await cur.fetchone()


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #

async def insert_event(
    cur,
    *,
    site_id: str,
    anonymous_id: str,
    lead_id: Optional[int],
    event_type: str,
    url: Optional[str],
    page_type: Optional[str],
    session_id: Optional[str],
    metadata: Optional[dict[str, Any]],
    occurred_at: datetime,
) -> dict:
    await cur.execute(
        """
        INSERT INTO events
            (site_id, anonymous_id, lead_id, event_type, url, page_type, session_id, metadata, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, occurred_at, received_at
        """,
        (
            site_id,
            anonymous_id,
            lead_id,
            event_type,
            url,
            page_type,
            session_id,
            Jsonb(metadata or {}),
            occurred_at,
        ),
    )
    return await cur.fetchone()


async def backfill_events_to_lead(cur, site_id: str, anonymous_id: str, lead_id: int) -> int:
    """Point ALL of an anonymous_id's prior events at the lead. Returns rows changed."""
    await cur.execute(
        """
        UPDATE events
        SET lead_id = %s
        WHERE site_id = %s AND anonymous_id = %s AND lead_id IS DISTINCT FROM %s
        """,
        (lead_id, site_id, anonymous_id, lead_id),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- #
# leads
# --------------------------------------------------------------------------- #

async def find_lead_by_email(cur, site_id: str, email: str) -> Optional[dict]:
    await cur.execute(
        "SELECT * FROM leads WHERE site_id = %s AND lower(email) = lower(%s) LIMIT 1",
        (site_id, email),
    )
    return await cur.fetchone()


async def find_lead_by_phone_exact(cur, site_id: str, phone: str) -> Optional[dict]:
    await cur.execute(
        "SELECT * FROM leads WHERE site_id = %s AND phone = %s LIMIT 1",
        (site_id, phone),
    )
    return await cur.fetchone()


async def find_lead_by_phone(cur, site_id: str, phone: str) -> Optional[dict]:
    """Match a lead by phone, comparing digits only.

    The same person reaches us as '+91 96995 30806' from a form and '919699530806'
    from WhatsApp. Comparing the raw strings would treat those as two people and
    create a duplicate lead, so every lookup normalizes first.
    """
    import re as _re

    digits = _re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    return await find_lead_by_phone_digits(cur, site_id, digits)


async def create_lead(
    cur,
    *,
    site_id: str,
    email: Optional[str],
    phone: Optional[str],
    email_opt_in: bool,
    whatsapp_opt_in: bool,
    consent_timestamp: datetime,
    consent_source: Optional[str],
    name: Optional[str] = None,
) -> dict:
    await cur.execute(
        """
        INSERT INTO leads
            (site_id, name, email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (site_id, name, email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source),
    )
    return await cur.fetchone()


async def update_lead_consent(
    cur,
    *,
    lead_id: int,
    email: Optional[str],
    phone: Optional[str],
    email_opt_in: bool,
    whatsapp_opt_in: bool,
    consent_timestamp: datetime,
    consent_source: Optional[str],
    name: Optional[str] = None,
) -> dict:
    """Refresh consent (latest wins), fill in email/phone if missing, update name."""
    await cur.execute(
        """
        UPDATE leads
        SET name              = COALESCE(%s, name),
            email             = COALESCE(email, %s),
            phone             = COALESCE(phone, %s),
            email_opt_in      = %s,
            whatsapp_opt_in   = %s,
            consent_timestamp = %s,
            consent_source    = %s
        WHERE id = %s
        RETURNING *
        """,
        (name, email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source, lead_id),
    )
    return await cur.fetchone()


async def get_lead_by_id(cur, site_id: str, lead_id: int) -> Optional[dict]:
    await cur.execute("SELECT * FROM leads WHERE site_id = %s AND id = %s", (site_id, lead_id))
    return await cur.fetchone()


async def create_anonymous_lead(cur, site_id: str) -> dict:
    """Create a lightweight profile lead (no email/phone yet) for an anonymous
    visitor. identify later enriches this same row."""
    await cur.execute("INSERT INTO leads (site_id) VALUES (%s) RETURNING *", (site_id,))
    return await cur.fetchone()


async def delete_lead_if_anonymous_orphan(cur, site_id: str, lead_id: int, keep_anonymous_id: str) -> int:
    """Delete an anonymous profile lead only if it has no contact info and no
    other identity still points at it. Used when merging a device into an
    already-identified lead. Returns rows deleted (0 or 1)."""
    await cur.execute(
        """
        DELETE FROM leads
        WHERE id = %s AND site_id = %s AND email IS NULL AND phone IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM identities
              WHERE lead_id = %s AND anonymous_id <> %s
          )
        """,
        (lead_id, site_id, lead_id, keep_anonymous_id),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- #
# scoring (Layer 2)
# --------------------------------------------------------------------------- #

async def list_unlinked_anonymous_ids(cur, site_id: str) -> list[str]:
    """anonymous_ids that have never been linked to a lead (need a profile)."""
    await cur.execute(
        "SELECT anonymous_id FROM identities WHERE site_id = %s AND lead_id IS NULL",
        (site_id,),
    )
    return [r["anonymous_id"] for r in await cur.fetchall()]


async def list_lead_ids(cur, site_id: str) -> list[int]:
    await cur.execute("SELECT id FROM leads WHERE site_id = %s ORDER BY id", (site_id,))
    return [r["id"] for r in await cur.fetchall()]


async def upsert_presence(
    cur, *, site_id: str, anonymous_id: str, session_id, url, page_type, last_seen
) -> None:
    """Record that a visitor is currently active (one row per visitor, updated in place)."""
    await cur.execute(
        """
        INSERT INTO visitor_presence (site_id, anonymous_id, session_id, url, page_type, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (site_id, anonymous_id) DO UPDATE
          SET session_id   = EXCLUDED.session_id,
              url          = EXCLUDED.url,
              page_type    = EXCLUDED.page_type,
              last_seen_at = EXCLUDED.last_seen_at
        """,
        (site_id, anonymous_id, session_id, url, page_type, last_seen),
    )


# --------------------------------------------------------------------------- #
# insights / page analytics
# --------------------------------------------------------------------------- #

# strip scheme+host, query AND fragment -> just the path, so /pricing?x=1 and
# /pricing#plans both group with /pricing (an anchor jump is the same page).
_PATH_SQL = (
    "NULLIF(split_part(split_part(regexp_replace(url, '^https?://[^/]+', ''), '?', 1), '#', 1), '')"
)


async def insights_totals(cur, site_id: str) -> dict:
    await cur.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE event_type = 'page_view')                          AS pageviews,
          count(*) FILTER (WHERE event_type NOT IN
                           ('page_view', 'heartbeat', 'page_engagement'))            AS clicks,
          count(*) FILTER (WHERE event_type <> 'page_engagement')                    AS events,
          count(DISTINCT anonymous_id)                                               AS visitors,
          count(DISTINCT session_id)                                                 AS sessions,
          count(DISTINCT {_PATH_SQL}) FILTER (WHERE event_type = 'page_view')        AS pages
        FROM events
        WHERE site_id = %s
        """,
        (site_id,),
    )
    return await cur.fetchone()


async def page_views_breakdown(cur, site_id: str) -> list[dict]:
    """Per-page views + unique visitors, most-viewed first."""
    await cur.execute(
        f"""
        SELECT COALESCE({_PATH_SQL}, '/') AS path,
               min(page_type)             AS page_type,
               count(*)                   AS views,
               count(DISTINCT anonymous_id) AS visitors
        FROM events
        WHERE site_id = %s AND event_type = 'page_view' AND url IS NOT NULL
        GROUP BY 1
        ORDER BY views DESC, path
        """,
        (site_id,),
    )
    return await cur.fetchall()


async def page_type_breakdown(cur, site_id: str) -> list[dict]:
    await cur.execute(
        """
        SELECT COALESCE(page_type, 'other') AS page_type,
               count(*)                      AS views,
               count(DISTINCT anonymous_id)  AS visitors
        FROM events
        WHERE site_id = %s AND event_type = 'page_view'
        GROUP BY 1
        ORDER BY views DESC
        """,
        (site_id,),
    )
    return await cur.fetchall()


async def views_by_day(cur, site_id: str, days: int) -> list[dict]:
    await cur.execute(
        """
        SELECT to_char(date_trunc('day', occurred_at), 'YYYY-MM-DD') AS day,
               count(*)                                              AS views,
               count(DISTINCT anonymous_id)                          AS visitors
        FROM events
        WHERE site_id = %s AND event_type = 'page_view'
          AND occurred_at > (now() - make_interval(days => %s))
        GROUP BY 1
        ORDER BY 1
        """,
        (site_id, days),
    )
    return await cur.fetchall()


async def get_site_config(cur, site_id: str) -> dict:
    """All DB config overrides for a site, as {key: value}."""
    await cur.execute("SELECT key, value FROM site_config WHERE site_id = %s", (site_id,))
    return {r["key"]: r["value"] for r in await cur.fetchall()}


async def upsert_site_config(cur, site_id: str, key: str, value) -> None:
    await cur.execute(
        """
        INSERT INTO site_config (site_id, key, value) VALUES (%s, %s, %s)
        ON CONFLICT (site_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (site_id, key, Jsonb(value or {})),
    )


async def list_map_visitors(cur, site_id: str, limit: int = 1000) -> list[dict]:
    """Visitors that have a geo fix (lat/lng), newest-seen first — for the world map."""
    await cur.execute(
        """
        SELECT p.anonymous_id, p.last_seen_at, p.url, p.page_type,
               i.city, i.region, i.country, i.district, i.street, i.postal,
               i.isp, i.accuracy_m, i.location_source, i.latitude, i.longitude,
               l.funnel_stage, l.intent_score, l.name, l.email
        FROM visitor_presence p
        JOIN identities i ON i.site_id = p.site_id AND i.anonymous_id = p.anonymous_id
        LEFT JOIN leads l ON l.site_id = p.site_id AND l.id = i.lead_id
        WHERE p.site_id = %s AND i.latitude IS NOT NULL AND i.longitude IS NOT NULL
        ORDER BY p.last_seen_at DESC
        LIMIT %s
        """,
        (site_id, limit),
    )
    return await cur.fetchall()


async def list_active_visitors(cur, site_id: str, since: datetime) -> list[dict]:
    """Visitors seen since `since`, newest first, enriched with lead stage if known."""
    await cur.execute(
        """
        SELECT p.anonymous_id, p.url, p.page_type, p.last_seen_at,
               i.lead_id, i.country, i.region, i.city, i.district, i.street,
               i.postal, i.isp, i.accuracy_m, i.location_source, i.timezone,
               l.funnel_stage, l.intent_score, l.email, l.name
        FROM visitor_presence p
        LEFT JOIN identities i ON i.site_id = p.site_id AND i.anonymous_id = p.anonymous_id
        LEFT JOIN leads l      ON l.site_id = p.site_id AND l.id = i.lead_id
        WHERE p.site_id = %s AND p.last_seen_at > %s
        ORDER BY p.last_seen_at DESC
        """,
        (site_id, since),
    )
    return await cur.fetchall()


RESCORE_ENGAGEMENT_SECONDS = 60


async def list_leads_needing_score(cur, site_id: str) -> list[int]:
    """Leads that are unscored, or whose behavior has actually moved since.

    A new page view or click always counts. Engagement pings do NOT count one by
    one — an open tab emits them every 30s, and re-classifying on each would mean
    an LLM call per cycle per open tab. They only trigger a re-score once enough
    active time has accumulated to plausibly change the answer.
    """
    await cur.execute(
        """
        SELECT l.id
        FROM leads l
        WHERE l.site_id = %s
          AND (
            l.scored_at IS NULL
            OR EXISTS (
                SELECT 1 FROM events e
                WHERE e.site_id = l.site_id AND e.lead_id = l.id
                  AND e.received_at > l.scored_at
                  AND e.event_type <> 'page_engagement'
            )
            OR COALESCE((
                SELECT sum((e.metadata ->> 'active_ms')::numeric)
                FROM events e
                WHERE e.site_id = l.site_id AND e.lead_id = l.id
                  AND e.received_at > l.scored_at
                  AND e.event_type = 'page_engagement'
                  AND jsonb_typeof(e.metadata -> 'active_ms') = 'number'
            ), 0) >= %s
          )
        ORDER BY l.id
        """,
        (site_id, RESCORE_ENGAGEMENT_SECONDS * 1000),
    )
    return [r["id"] for r in await cur.fetchall()]


async def list_leads_needing_decision(cur, site_id: str) -> list[int]:
    """Scored leads with no decision yet, or (re)scored after their last decision.

    So a decision is (re)made only when a lead's score is newer than its last
    decision — new/changed visitors get decided, stable ones don't re-spam.
    """
    await cur.execute(
        """
        SELECT l.id
        FROM leads l
        WHERE l.site_id = %s
          AND l.scored_at IS NOT NULL
          AND (
            NOT EXISTS (
                SELECT 1 FROM decisions d
                WHERE d.site_id = l.site_id AND d.lead_id = l.id
            )
            OR l.scored_at > (
                SELECT max(d.created_at) FROM decisions d
                WHERE d.site_id = l.site_id AND d.lead_id = l.id
            )
          )
        ORDER BY l.id
        """,
        (site_id,),
    )
    return [r["id"] for r in await cur.fetchall()]


async def list_events_for_lead(cur, site_id: str, lead_id: int, limit: int = 1000) -> list[dict]:
    """Human-readable event timeline for a lead (oldest first) — the journey view.

    Engagement pings are excluded IN SQL: they are plumbing, and filtering them
    after the LIMIT would let them crowd real page views out of the timeline.
    Newest rows are kept, then re-sorted oldest-first for display.
    """
    await cur.execute(
        """
        SELECT * FROM (
            SELECT id, event_type, url, page_type, session_id, metadata, occurred_at
            FROM events
            WHERE site_id = %s AND lead_id = %s
              AND event_type NOT IN ('page_engagement', 'heartbeat')
            ORDER BY occurred_at DESC, id DESC
            LIMIT %s
        ) recent
        ORDER BY occurred_at ASC, id ASC
        """,
        (site_id, lead_id, limit),
    )
    return await cur.fetchall()


async def lead_last_presence(cur, site_id: str, lead_id: int):
    """Most recent 'live' heartbeat across all of a lead's anonymous ids (or None)."""
    await cur.execute(
        """
        SELECT max(p.last_seen_at) AS last_seen
        FROM visitor_presence p
        JOIN identities i ON i.site_id = p.site_id AND i.anonymous_id = p.anonymous_id
        WHERE p.site_id = %s AND i.lead_id = %s
        """,
        (site_id, lead_id),
    )
    row = await cur.fetchone()
    return row["last_seen"] if row else None


async def event_aggregates(cur, site_id: str, lead_id: int) -> dict:
    await cur.execute(
        """
        SELECT count(*)                                          AS events,
               count(DISTINCT session_id)                        AS sessions,
               count(*) FILTER (WHERE event_type = 'page_view')  AS pageviews,
               count(DISTINCT date_trunc('day', occurred_at))    AS active_days,
               min(occurred_at)                                  AS first_at,
               max(occurred_at)                                  AS last_at
        FROM events
        WHERE site_id = %s AND lead_id = %s
          AND event_type NOT IN ('heartbeat', 'page_engagement')
        """,
        (site_id, lead_id),
    )
    return await cur.fetchone()


async def event_counts_by_page_type(cur, site_id: str, lead_id: int) -> dict:
    """Page VIEWS per page type (clicks and engagement pings excluded — counting
    those here would let one chatty page masquerade as many visits)."""
    await cur.execute(
        """
        SELECT page_type, count(*) AS n
        FROM events
        WHERE site_id = %s AND lead_id = %s AND page_type IS NOT NULL
          AND event_type = 'page_view'
        GROUP BY page_type
        """,
        (site_id, lead_id),
    )
    return {r["page_type"]: r["n"] for r in await cur.fetchall()}


async def lead_behavior_rows(cur, site_id: str, lead_id: int, limit: int = 5000) -> list[dict]:
    """Every event for a lead, reduced to what the rules engine needs.

    `vid` (visit id, set by the tracking snippet) ties clicks and engagement
    pings back to the exact page view they belong to, which is what makes
    per-visit dwell — "45 active seconds on /pricing" — measurable at all.
    """
    # Take the NEWEST rows and re-sort them oldest-first: a plain ASC LIMIT would
    # freeze a heavy visitor's score on their first 5000 events forever.
    await cur.execute(
        f"""
        SELECT * FROM (
            SELECT event_type,
                   page_type,
                   COALESCE({_PATH_SQL}, '/')        AS path,
                   session_id,
                   metadata ->> 'vid'                AS vid,
                   metadata,
                   occurred_at,
                   id
            FROM events
            WHERE site_id = %s AND lead_id = %s AND event_type <> 'heartbeat'
            ORDER BY occurred_at DESC, id DESC
            LIMIT %s
        ) recent
        ORDER BY occurred_at ASC, id ASC
        """,
        (site_id, lead_id, limit),
    )
    return await cur.fetchall()


async def observed_vocabulary(cur, site_id: str, limit: int = 60) -> dict:
    """Paths, page types, section names and click labels actually seen on this
    site — used to populate the rule-builder dropdowns with real values."""
    await cur.execute(
        f"""
        SELECT COALESCE({_PATH_SQL}, '/') AS path, count(*) AS n
        FROM events WHERE site_id = %s AND event_type = 'page_view'
        GROUP BY 1 ORDER BY n DESC LIMIT %s
        """,
        (site_id, limit),
    )
    paths = [r["path"] for r in await cur.fetchall()]

    await cur.execute(
        """
        SELECT DISTINCT page_type FROM events
        WHERE site_id = %s AND page_type IS NOT NULL ORDER BY 1
        """,
        (site_id,),
    )
    page_types = [r["page_type"] for r in await cur.fetchall()]

    await cur.execute(
        """
        SELECT s.key AS name, count(*) AS n
        FROM events e, jsonb_each(COALESCE(e.metadata -> 'sections', '{}'::jsonb)) AS s
        WHERE e.site_id = %s AND e.event_type = 'page_engagement'
        GROUP BY 1 ORDER BY n DESC LIMIT %s
        """,
        (site_id, limit),
    )
    sections = [r["name"] for r in await cur.fetchall()]

    await cur.execute(
        """
        SELECT DISTINCT event_type FROM events
        WHERE site_id = %s AND event_type NOT IN ('page_view', 'heartbeat', 'page_engagement')
        ORDER BY 1 LIMIT %s
        """,
        (site_id, limit),
    )
    event_types = [r["event_type"] for r in await cur.fetchall()]

    await cur.execute(
        """
        SELECT lower(metadata ->> 'text') AS label, count(*) AS n
        FROM events
        WHERE site_id = %s AND metadata ->> 'text' IS NOT NULL AND metadata ->> 'text' <> ''
          AND event_type NOT IN ('page_view', 'heartbeat', 'page_engagement')
        GROUP BY 1 ORDER BY n DESC LIMIT %s
        """,
        (site_id, limit),
    )
    click_labels = [r["label"] for r in await cur.fetchall()]

    return {
        "paths": paths,
        "page_types": page_types,
        "sections": sections,
        "event_types": event_types,
        "click_labels": click_labels,
    }


async def event_counts_for_types(cur, site_id: str, lead_id: int, event_types: list[str]) -> dict:
    if not event_types:
        return {}
    await cur.execute(
        """
        SELECT event_type, count(*) AS n
        FROM events
        WHERE site_id = %s AND lead_id = %s AND event_type = ANY(%s)
        GROUP BY event_type
        """,
        (site_id, lead_id, event_types),
    )
    return {r["event_type"]: r["n"] for r in await cur.fetchall()}


async def first_event(cur, site_id: str, lead_id: int) -> Optional[dict]:
    await cur.execute(
        """
        SELECT event_type, url, metadata, occurred_at
        FROM events
        WHERE site_id = %s AND lead_id = %s
        ORDER BY occurred_at ASC LIMIT 1
        """,
        (site_id, lead_id),
    )
    return await cur.fetchone()


async def recent_events(cur, site_id: str, lead_id: int, limit: int) -> list[dict]:
    """Most recent real events. Engagement pings are excluded IN SQL — filtering
    them after the LIMIT could hand the model an empty list for exactly the
    visitors who are reading the most."""
    await cur.execute(
        """
        SELECT event_type, url, page_type, session_id, occurred_at, metadata
        FROM events
        WHERE site_id = %s AND lead_id = %s
          AND event_type NOT IN ('page_engagement', 'heartbeat')
        ORDER BY occurred_at DESC LIMIT %s
        """,
        (site_id, lead_id, limit),
    )
    return await cur.fetchall()


async def update_lead_score(
    cur,
    *,
    lead_id: int,
    funnel_stage: Optional[str],
    intent_score: Optional[int],
    likely_objections,
    persona_signals,
    scored_at: datetime,
    scoring_error: Optional[str],
    stage_source: Optional[str] = None,
    stage_reason: Optional[str] = None,
) -> dict:
    await cur.execute(
        """
        UPDATE leads
        SET funnel_stage      = %s,
            intent_score      = %s,
            likely_objections = %s,
            persona_signals   = %s,
            scored_at         = %s,
            scoring_error     = %s,
            stage_source      = %s,
            stage_reason      = %s
        WHERE id = %s
        RETURNING id
        """,
        (
            funnel_stage,
            intent_score,
            Jsonb(likely_objections or []),
            Jsonb(persona_signals or {}),
            scored_at,
            scoring_error,
            stage_source,
            stage_reason,
            lead_id,
        ),
    )
    return await cur.fetchone()


# --------------------------------------------------------------------------- #
# decisions (Layer 3)
# --------------------------------------------------------------------------- #

async def count_recent_outreach(cur, site_id: str, lead_id: int, actions: list[str], since: datetime) -> int:
    """Accepted outreach decisions for a lead since `since` — feeds the rate limit."""
    if not actions:
        return 0
    await cur.execute(
        """
        SELECT count(*) AS n
        FROM decisions
        WHERE site_id = %s AND lead_id = %s AND status = 'accepted'
          AND action = ANY(%s) AND created_at >= %s
        """,
        (site_id, lead_id, actions, since),
    )
    row = await cur.fetchone()
    return row["n"]


async def insert_decision(
    cur,
    *,
    site_id: str,
    lead_id: int,
    action: str,
    channel: Optional[str],
    message: Optional[str],
    send_at: Optional[datetime],
    reasoning: Optional[str],
    status: str,
    guardrail_result: dict,
    model: Optional[str],
    raw_response: Optional[dict],
) -> dict:
    await cur.execute(
        """
        INSERT INTO decisions
            (site_id, lead_id, action, channel, message, send_at, reasoning,
             status, guardrail_result, model, raw_response)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (
            site_id,
            lead_id,
            action,
            channel,
            message,
            send_at,
            reasoning,
            status,
            Jsonb(guardrail_result or {}),
            model,
            Jsonb(raw_response) if raw_response is not None else None,
        ),
    )
    return await cur.fetchone()


async def get_decision_by_id(cur, site_id: str, decision_id: int) -> Optional[dict]:
    await cur.execute("SELECT * FROM decisions WHERE site_id = %s AND id = %s", (site_id, decision_id))
    return await cur.fetchone()


# --------------------------------------------------------------------------- #
# sent_messages (Layer 4 — stubbed execution log)
# --------------------------------------------------------------------------- #

async def list_pending_outreach_decisions(cur, site_id: str, outreach_actions: list[str]) -> list[int]:
    """Accepted outreach decisions with no sent_messages row yet (never executed)."""
    if not outreach_actions:
        return []
    await cur.execute(
        """
        SELECT d.id
        FROM decisions d
        WHERE d.site_id = %s AND d.status = 'accepted' AND d.action = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM sent_messages s WHERE s.decision_id = d.id)
        ORDER BY d.id
        """,
        (site_id, outreach_actions),
    )
    return [r["id"] for r in await cur.fetchall()]


async def sent_message_exists_for_decision(cur, site_id: str, decision_id: int) -> bool:
    await cur.execute(
        "SELECT 1 FROM sent_messages WHERE site_id = %s AND decision_id = %s LIMIT 1",
        (site_id, decision_id),
    )
    return await cur.fetchone() is not None


async def insert_sent_message(
    cur,
    *,
    site_id: str,
    lead_id: int,
    decision_id: Optional[int],
    channel: str,
    sender_type: Optional[str],
    to_address: Optional[str],
    message: Optional[str],
    metadata: dict,
    status: str,
    skip_reason: Optional[str],
) -> dict:
    await cur.execute(
        """
        INSERT INTO sent_messages
            (site_id, lead_id, decision_id, channel, sender_type, to_address,
             message, metadata, status, skip_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (
            site_id,
            lead_id,
            decision_id,
            channel,
            sender_type,
            to_address,
            message,
            Jsonb(metadata or {}),
            status,
            skip_reason,
        ),
    )
    return await cur.fetchone()


# --------------------------------------------------------------------------- #
# dashboard reads (Layer 5, read-only)
# --------------------------------------------------------------------------- #

async def list_leads(cur, site_id: str) -> list[dict]:
    await cur.execute(
        """
        SELECT l.id, l.name, l.email, l.phone, l.email_opt_in, l.whatsapp_opt_in, l.consent_source,
               l.funnel_stage, l.intent_score, l.likely_objections, l.persona_signals,
               l.scored_at, l.scoring_error, l.stage_source, l.stage_reason, l.created_at,
               g.country, g.region, g.city, g.timezone,
               COALESCE(
                 (SELECT max(e.occurred_at) FROM events e WHERE e.site_id = l.site_id AND e.lead_id = l.id),
                 l.created_at
               ) AS last_activity
        FROM leads l
        LEFT JOIN LATERAL (
            SELECT country, region, city, district, postal, isp,
                   accuracy_m, location_source, timezone
            FROM identities i
            WHERE i.site_id = l.site_id AND i.lead_id = l.id
              AND (i.country IS NOT NULL OR i.city IS NOT NULL OR i.timezone IS NOT NULL)
            ORDER BY (i.location_source = 'gps') DESC NULLS LAST, i.id DESC LIMIT 1
        ) g ON true
        WHERE l.site_id = %s
        ORDER BY last_activity DESC, l.id DESC
        """,
        (site_id,),
    )
    return await cur.fetchall()


async def list_decisions(cur, site_id: str) -> list[dict]:
    await cur.execute(
        """
        SELECT d.id, d.lead_id, l.email AS lead_email, d.action, d.channel,
               d.status, d.send_at, d.reasoning, d.guardrail_result, d.model, d.created_at
        FROM decisions d
        JOIN leads l ON l.id = d.lead_id
        WHERE d.site_id = %s
        ORDER BY d.created_at DESC, d.id DESC
        """,
        (site_id,),
    )
    return await cur.fetchall()


async def list_sent_messages(cur, site_id: str) -> list[dict]:
    await cur.execute(
        """
        SELECT s.id, s.lead_id, l.email AS lead_email, s.channel, s.sender_type,
               s.to_address, s.status, s.skip_reason, s.message, s.decision_id, s.created_at
        FROM sent_messages s
        JOIN leads l ON l.id = s.lead_id
        WHERE s.site_id = %s
        ORDER BY s.created_at DESC, s.id DESC
        """,
        (site_id,),
    )
    return await cur.fetchall()


# --------------------------------------------------------------------------- #
# conversations + messages (Layer 4 — two-way messaging)
# --------------------------------------------------------------------------- #

async def get_or_create_conversation(
    cur, site_id: str, *, channel: str, contact: str, lead_id: Optional[int] = None
) -> dict:
    """Return the thread for (channel, contact), creating it if new.

    The no-op DO UPDATE makes RETURNING yield the existing row on conflict. A
    lead_id is only ever filled in, never cleared — an inbound message can create
    the thread before we know who it is from.
    """
    await cur.execute(
        """
        INSERT INTO conversations (site_id, channel, contact, lead_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (site_id, channel, contact)
        DO UPDATE SET lead_id = COALESCE(conversations.lead_id, EXCLUDED.lead_id)
        RETURNING *
        """,
        (site_id, channel, contact, lead_id),
    )
    return await cur.fetchone()


async def attach_conversation_lead(cur, site_id: str, conversation_id: int, lead_id: int) -> None:
    await cur.execute(
        "UPDATE conversations SET lead_id = %s WHERE site_id = %s AND id = %s",
        (lead_id, site_id, conversation_id),
    )
    # Backfill the thread's history so earlier anonymous messages follow the lead.
    await cur.execute(
        "UPDATE messages SET lead_id = %s WHERE site_id = %s AND conversation_id = %s AND lead_id IS NULL",
        (lead_id, site_id, conversation_id),
    )


async def insert_message(
    cur,
    *,
    site_id: str,
    conversation_id: int,
    lead_id: Optional[int],
    direction: str,
    channel: str,
    body: Optional[str],
    message_type: str = "text",
    media: Optional[dict] = None,
    status: str = "queued",
    error: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    sender_type: Optional[str] = None,
    decision_id: Optional[int] = None,
    sent_message_id: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
) -> Optional[dict]:
    """Append a message. Returns None when provider_message_id was already stored
    (webhook retry) so callers can treat redelivery as a no-op."""
    await cur.execute(
        """
        INSERT INTO messages (site_id, conversation_id, lead_id, direction, channel, body,
                              message_type, media, status, error, provider_message_id,
                              sender_type, decision_id, sent_message_id, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
        ON CONFLICT (site_id, provider_message_id) WHERE provider_message_id IS NOT NULL
        DO NOTHING
        RETURNING *
        """,
        (site_id, conversation_id, lead_id, direction, channel, body, message_type,
         Jsonb(media or {}), status, error, provider_message_id, sender_type,
         decision_id, sent_message_id, occurred_at),
    )
    return await cur.fetchone()


async def touch_conversation(
    cur, site_id: str, conversation_id: int, *,
    direction: str, at: datetime, window_hours: int = 24, unread: bool = False,
) -> dict:
    """Advance a thread's activity clocks after a message.

    An inbound message also restarts the customer-service window — the only event
    that does. GREATEST(...) keeps the columns monotonic so an out-of-order
    webhook redelivery can't wind the clock backwards.
    """
    if direction == "in":
        await cur.execute(
            """
            UPDATE conversations
               SET last_inbound_at   = GREATEST(COALESCE(last_inbound_at, %s), %s),
                   last_message_at   = GREATEST(COALESCE(last_message_at, %s), %s),
                   window_expires_at = GREATEST(COALESCE(window_expires_at, %s),
                                                %s + make_interval(hours => %s)),
                   unread_count      = unread_count + %s,
                   status            = 'open'
             WHERE site_id = %s AND id = %s
            RETURNING *
            """,
            (at, at, at, at, at, at, window_hours, 1 if unread else 0, site_id, conversation_id),
        )
    else:
        await cur.execute(
            """
            UPDATE conversations
               SET last_outbound_at = GREATEST(COALESCE(last_outbound_at, %s), %s),
                   last_message_at  = GREATEST(COALESCE(last_message_at, %s), %s)
             WHERE site_id = %s AND id = %s
            RETURNING *
            """,
            (at, at, at, at, site_id, conversation_id),
        )
    return await cur.fetchone()


async def update_message_status(
    cur, site_id: str, provider_message_id: str, *,
    status: str, at: Optional[datetime] = None, error: Optional[str] = None,
) -> Optional[dict]:
    """Apply a provider delivery receipt.

    Status only moves FORWARD along queued -> sent -> delivered -> read: Meta
    delivers receipts out of order, and a late 'sent' must not undo a 'read'.
    'failed' always wins, since it is terminal.
    """
    await cur.execute(
        """
        UPDATE messages
           SET status = CASE
                          WHEN %s = 'failed' THEN 'failed'
                          WHEN messages.status = 'failed' THEN 'failed'
                          WHEN array_position(ARRAY['queued','sent','delivered','read'], %s)
                             > array_position(ARRAY['queued','sent','delivered','read'], messages.status)
                          THEN %s
                          ELSE messages.status
                        END,
               delivered_at = CASE WHEN %s IN ('delivered','read')
                                   THEN COALESCE(delivered_at, COALESCE(%s, now())) ELSE delivered_at END,
               read_at      = CASE WHEN %s = 'read'
                                   THEN COALESCE(read_at, COALESCE(%s, now())) ELSE read_at END,
               error        = COALESCE(%s, error)
         WHERE site_id = %s AND provider_message_id = %s
        RETURNING *
        """,
        (status, status, status, status, at, status, at, error, site_id, provider_message_id),
    )
    return await cur.fetchone()


async def finalize_message(
    cur, site_id: str, message_id: int, *,
    status: str, error: Optional[str] = None, provider_message_id: Optional[str] = None,
    sent_message_id: Optional[int] = None,
) -> Optional[dict]:
    """Stamp a queued outbound row with its provider id and send outcome.

    Never downgrades: if a delivery receipt already advanced this row past 'sent'
    in the moments after transmission, the late 'sent' must not undo it.
    """
    await cur.execute(
        """
        UPDATE messages
           SET provider_message_id = COALESCE(%s, provider_message_id),
               sent_message_id    = COALESCE(%s, sent_message_id),
               error  = COALESCE(%s, error),
               status = CASE
                          WHEN %s = 'failed' THEN 'failed'
                          WHEN array_position(ARRAY['queued','sent','delivered','read'], %s)
                             > array_position(ARRAY['queued','sent','delivered','read'], messages.status)
                          THEN %s
                          ELSE messages.status
                        END
         WHERE site_id = %s AND id = %s
        RETURNING *
        """,
        (provider_message_id, sent_message_id, error, status, status, status, site_id, message_id),
    )
    return await cur.fetchone()


async def park_receipt(
    cur, site_id: str, provider_message_id: str, *,
    status: str, error: Optional[str] = None, at: Optional[datetime] = None,
) -> None:
    """Hold a receipt whose message row is not addressable yet.

    Keeps the furthest-along status if several arrive before the message lands.
    """
    await cur.execute(
        """
        INSERT INTO pending_receipts (site_id, provider_message_id, status, error, occurred_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (site_id, provider_message_id) DO UPDATE
        SET status = CASE
                       WHEN EXCLUDED.status = 'failed' THEN 'failed'
                       WHEN pending_receipts.status = 'failed' THEN 'failed'
                       WHEN array_position(ARRAY['queued','sent','delivered','read'], EXCLUDED.status)
                          > array_position(ARRAY['queued','sent','delivered','read'], pending_receipts.status)
                       THEN EXCLUDED.status
                       ELSE pending_receipts.status
                     END,
            error       = COALESCE(EXCLUDED.error, pending_receipts.error),
            occurred_at = COALESCE(EXCLUDED.occurred_at, pending_receipts.occurred_at)
        """,
        (site_id, provider_message_id, status, error, at),
    )


async def take_parked_receipt(cur, site_id: str, provider_message_id: str) -> Optional[dict]:
    """Pop a parked receipt, if one is waiting for this provider id."""
    await cur.execute(
        "DELETE FROM pending_receipts WHERE site_id = %s AND provider_message_id = %s RETURNING *",
        (site_id, provider_message_id),
    )
    return await cur.fetchone()


async def mark_conversation_read(cur, site_id: str, conversation_id: int) -> None:
    await cur.execute(
        "UPDATE conversations SET unread_count = 0 WHERE site_id = %s AND id = %s",
        (site_id, conversation_id),
    )


async def get_conversation(cur, site_id: str, conversation_id: int) -> Optional[dict]:
    await cur.execute(
        "SELECT * FROM conversations WHERE site_id = %s AND id = %s", (site_id, conversation_id)
    )
    return await cur.fetchone()


async def list_conversations(cur, site_id: str, limit: int = 200) -> list[dict]:
    """Inbox list: newest activity first, with the last message inlined."""
    await cur.execute(
        """
        SELECT c.*, l.name AS lead_name, l.email AS lead_email,
               l.funnel_stage, l.intent_score,
               m.body AS last_body, m.direction AS last_direction, m.status AS last_status,
               (c.window_expires_at IS NOT NULL AND c.window_expires_at > now()) AS window_open
        FROM conversations c
        LEFT JOIN leads l ON l.site_id = c.site_id AND l.id = c.lead_id
        LEFT JOIN LATERAL (
            SELECT body, direction, status FROM messages m
            WHERE m.conversation_id = c.id
            ORDER BY m.occurred_at DESC, m.id DESC LIMIT 1
        ) m ON true
        WHERE c.site_id = %s
        ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC
        LIMIT %s
        """,
        (site_id, limit),
    )
    return await cur.fetchall()


async def list_messages(cur, site_id: str, conversation_id: int, limit: int = 500) -> list[dict]:
    """Thread contents, oldest first. Newest rows are kept when truncating."""
    await cur.execute(
        """
        SELECT * FROM (
            SELECT id, direction, channel, body, message_type, media, status, error,
                   provider_message_id, sender_type, decision_id,
                   occurred_at, delivered_at, read_at
            FROM messages
            WHERE site_id = %s AND conversation_id = %s
            ORDER BY occurred_at DESC, id DESC
            LIMIT %s
        ) t ORDER BY occurred_at ASC, id ASC
        """,
        (site_id, conversation_id, limit),
    )
    return await cur.fetchall()


async def find_lead_by_phone_digits(cur, site_id: str, digits: str) -> Optional[dict]:
    """Match a lead by phone ignoring '+', spaces and punctuation.

    Inbound WhatsApp gives bare E.164 digits while leads are stored however the
    form captured them, so a literal comparison would miss almost every match.
    """
    if not digits:
        return None
    await cur.execute(
        """
        SELECT * FROM leads
        WHERE site_id = %s AND phone IS NOT NULL
          AND regexp_replace(phone, '\\D', '', 'g') = %s
        ORDER BY id LIMIT 1
        """,
        (site_id, digits),
    )
    return await cur.fetchone()
