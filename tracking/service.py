"""Layer 1 business logic: event ingest, page_type resolution (via config),
identity linking, and event backfill on identify.

Kept free of HTTP concerns (routers translate to/from these functions) and free
of SQL (repositories own that). Business rules for page typing live in config.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from config.loader import resolve_page_type
from db import repositories as repo
from db.connection import transaction


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
) -> dict:
    """Ingest one event. If the anonymous_id is already identified, the event is
    attributed to that lead immediately (live attribution after identify)."""
    occurred_at = _ensure_utc(timestamp)
    page_type, _lean = resolve_page_type(url, site_id)

    async with transaction() as cur:
        identity = await repo.get_or_create_identity(cur, site_id, anonymous_id)
        lead_id = identity.get("lead_id")
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

    return {
        "event_id": event["id"],
        "lead_id": lead_id,
        "page_type": page_type,
    }


async def identify(
    *,
    site_id: str,
    anonymous_id: str,
    email: Optional[str],
    phone: Optional[str],
    email_opt_in: bool,
    whatsapp_opt_in: bool,
    consent_timestamp: Optional[datetime],
    consent_source: Optional[str],
) -> dict:
    """Resolve the anonymous_id to a lead, refresh consent, backfill events.

    Resolution precedence keeps one lead per person while honoring an anonymous
    profile that scoring may already have created for this visitor:
      1. An existing identified lead matched by email/phone wins.
      2. Else the anonymous_id's current profile lead (if any) is enriched.
      3. Else a new lead is created.
    A stale anonymous profile displaced by case 1 is deleted if unused. Atomic.
    """
    consent_ts = _ensure_utc(consent_timestamp)

    async with transaction() as cur:
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
            )
        else:
            created = True
            lead = await repo.create_lead(
                cur,
                site_id=site_id,
                email=email,
                phone=phone,
                email_opt_in=email_opt_in,
                whatsapp_opt_in=whatsapp_opt_in,
                consent_timestamp=consent_ts,
                consent_source=consent_source,
            )

        await repo.link_identity(cur, site_id, anonymous_id, lead["id"])
        backfilled = await repo.backfill_events_to_lead(cur, site_id, anonymous_id, lead["id"])

    return {
        "lead_id": lead["id"],
        "created": created,
        "anonymous_id": anonymous_id,
        "backfilled_events": backfilled,
    }
