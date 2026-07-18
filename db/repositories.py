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
        RETURNING id, site_id, anonymous_id, lead_id
        """,
        (site_id, anonymous_id),
    )
    return await cur.fetchone()


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


async def find_lead_by_phone(cur, site_id: str, phone: str) -> Optional[dict]:
    await cur.execute(
        "SELECT * FROM leads WHERE site_id = %s AND phone = %s LIMIT 1",
        (site_id, phone),
    )
    return await cur.fetchone()


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
) -> dict:
    await cur.execute(
        """
        INSERT INTO leads
            (site_id, email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (site_id, email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source),
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
) -> dict:
    """Refresh consent (latest wins) and fill in email/phone if still missing."""
    await cur.execute(
        """
        UPDATE leads
        SET email             = COALESCE(email, %s),
            phone             = COALESCE(phone, %s),
            email_opt_in      = %s,
            whatsapp_opt_in   = %s,
            consent_timestamp = %s,
            consent_source    = %s
        WHERE id = %s
        RETURNING *
        """,
        (email, phone, email_opt_in, whatsapp_opt_in, consent_timestamp, consent_source, lead_id),
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
        """,
        (site_id, lead_id),
    )
    return await cur.fetchone()


async def event_counts_by_page_type(cur, site_id: str, lead_id: int) -> dict:
    await cur.execute(
        """
        SELECT page_type, count(*) AS n
        FROM events
        WHERE site_id = %s AND lead_id = %s AND page_type IS NOT NULL
        GROUP BY page_type
        """,
        (site_id, lead_id),
    )
    return {r["page_type"]: r["n"] for r in await cur.fetchall()}


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
    await cur.execute(
        """
        SELECT event_type, url, page_type, session_id, occurred_at, metadata
        FROM events
        WHERE site_id = %s AND lead_id = %s
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
) -> dict:
    await cur.execute(
        """
        UPDATE leads
        SET funnel_stage      = %s,
            intent_score      = %s,
            likely_objections = %s,
            persona_signals   = %s,
            scored_at         = %s,
            scoring_error     = %s
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
        SELECT id, email, phone, email_opt_in, whatsapp_opt_in, consent_source,
               funnel_stage, intent_score, likely_objections, persona_signals,
               scored_at, scoring_error, created_at
        FROM leads
        WHERE site_id = %s
        ORDER BY intent_score DESC NULLS LAST, id
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
