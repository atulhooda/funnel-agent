"""Two-way messaging — conversations, inbound capture, delivery state.

The execution layer (Layer 4) sends what the agent DECIDES to say. This module
owns the conversation around it: replies coming back, delivery receipts, and the
thread a human reads in the dashboard.

The rule that shapes everything here is WhatsApp's **24-hour customer-service
window**: free-form text may only be sent within 24 hours of the contact's own
last message. Outside it, Meta silently rejects text and requires an approved
template. That window is per-conversation state — it moves every time the
contact writes to you — so it is tracked on the conversation row and consulted
at send time, never guessed from static config.

Kept free of HTTP concerns (the router translates) and free of SQL (repositories
own that).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config.loader import get_config
from db import repositories as repo
from db.connection import transaction

WINDOW_HOURS = 24          # WhatsApp customer-service window
WHATSAPP = "whatsapp"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_contact(channel: str, value: Optional[str]) -> str:
    """Canonical thread key for an address.

    WhatsApp numbers arrive as bare E.164 digits from Meta but are stored on a
    lead however the form captured them ('+91 96995 30806'), so phone contacts
    are reduced to digits. Email is lowercased.
    """
    text = (value or "").strip()
    if channel == WHATSAPP:
        return re.sub(r"\D", "", text)
    return text.lower()


def window_open(conversation: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True when free-form text is currently allowed on this thread.

    Only meaningful for WhatsApp; other channels have no such restriction.
    """
    if not conversation:
        return False
    if conversation.get("channel") != WHATSAPP:
        return True
    expires = conversation.get("window_expires_at")
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or _utcnow())


def window_state(conversation: Optional[dict], now: Optional[datetime] = None) -> dict:
    """Window status for the API: open/closed plus how long is left."""
    now = now or _utcnow()
    expires = conversation.get("window_expires_at") if conversation else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    is_open = window_open(conversation, now)
    return {
        "open": is_open,
        "expires_at": expires.isoformat() if expires else None,
        "seconds_left": int((expires - now).total_seconds()) if (expires and is_open) else 0,
        # What a send would have to use right now.
        "allowed_send": "text" if is_open else "template",
    }


async def ensure_conversation(
    site_id: str, *, channel: str, contact: str, lead_id: Optional[int] = None
) -> dict:
    """Get (or open) the thread for an address, attaching a lead when known."""
    key = normalize_contact(channel, contact)
    async with transaction() as cur:
        conversation = await repo.get_or_create_conversation(
            cur, site_id, channel=channel, contact=key, lead_id=lead_id
        )
        if lead_id and not conversation.get("lead_id"):
            await repo.attach_conversation_lead(cur, site_id, conversation["id"], lead_id)
            conversation["lead_id"] = lead_id
    return conversation


async def conversation_for_lead(site_id: str, lead_id: int, channel: str, contact: str) -> dict:
    return await ensure_conversation(site_id, channel=channel, contact=contact, lead_id=lead_id)


# --------------------------------------------------------------------------- #
# inbound
# --------------------------------------------------------------------------- #

def is_opt_out(body: Optional[str], site_id: str = "default") -> bool:
    """Is this inbound message a request to stop being messaged?

    Matched as the WHOLE message, not a substring: our own copy talks about
    clinics that "stop losing patients", and a reply quoting it must not silence
    the lead. Trailing punctuation and case are ignored, so "STOP." counts.
    """
    if not body:
        return False
    text = " ".join(body.strip().lower().split()).strip(".!,;: ")
    keywords = get_config("guardrails", site_id).get("opt_out_keywords") or []
    return text in {str(k).strip().lower() for k in keywords}


async def record_inbound(
    site_id: str,
    *,
    channel: str,
    contact: str,
    body: Optional[str],
    provider_message_id: Optional[str] = None,
    message_type: str = "text",
    media: Optional[dict] = None,
    occurred_at: Optional[datetime] = None,
    profile_name: Optional[str] = None,
) -> dict:
    """Store a message FROM a contact and reopen their service window.

    Resolves the sender to an existing lead by phone/email when possible; a
    thread with no lead is still recorded, because losing an inbound message is
    worse than not knowing who sent it.
    """
    key = normalize_contact(channel, contact)
    at = occurred_at or _utcnow()

    async with transaction() as cur:
        lead = None
        if channel == WHATSAPP:
            lead = await repo.find_lead_by_phone_digits(cur, site_id, key)
        else:
            lead = await repo.find_lead_by_email(cur, site_id, key)

        if lead is None and get_config("guardrails", site_id).get(
                "create_leads_from_inbound", False):
            # Someone who messages the business number IS a lead — usually a warm
            # one, since they made contact. Without this they would sit in the
            # inbox as a bare phone number: never scored, never decided on.
            #
            # Only when the funnel owns the number, though: on a shared one this
            # turns every patient chasing an appointment into a marketing lead.
            #
            # Consent is deliberately NOT granted here. Writing in authorizes a
            # reply (that is what Meta's 24h window is for) but not agent-initiated
            # outreach later, which the guardrails still require an opt-in for.
            lead = await repo.create_lead(
                cur,
                site_id=site_id,
                email=key if channel != WHATSAPP else None,
                phone=key if channel == WHATSAPP else None,
                email_opt_in=False,
                whatsapp_opt_in=False,
                consent_timestamp=at,
                consent_source=f"{channel}_inbound",
                name=profile_name,
            )
        lead_id = lead["id"] if lead else None

        conversation = await repo.get_or_create_conversation(
            cur, site_id, channel=channel, contact=key, lead_id=lead_id
        )
        if lead_id and not conversation.get("lead_id"):
            await repo.attach_conversation_lead(cur, site_id, conversation["id"], lead_id)

        # A name from the provider profile is better than nothing on an
        # otherwise anonymous lead, but never overwrites one they gave us.
        if lead_id and profile_name and not (lead or {}).get("name"):
            await cur.execute(
                "UPDATE leads SET name = COALESCE(name, %s) WHERE id = %s", (profile_name, lead_id)
            )

        message = await repo.insert_message(
            cur,
            site_id=site_id,
            conversation_id=conversation["id"],
            lead_id=lead_id,
            direction="in",
            channel=channel,
            body=body,
            message_type=message_type,
            media=media,
            status="received",
            provider_message_id=provider_message_id,
            occurred_at=at,
        )
        if message is None:
            # Provider redelivery of a message we already stored.
            return {"duplicate": True, "conversation_id": conversation["id"]}

        conversation = await repo.touch_conversation(
            cur, site_id, conversation["id"],
            direction="in", at=at, window_hours=WINDOW_HOURS, unread=True,
        )

        # An opt-out is honoured on the way IN, before anything else can be
        # queued for them. Recorded inside the same transaction as the message
        # so we can never store the "STOP" and miss acting on it.
        opted_out = channel == WHATSAPP and lead_id and is_opt_out(body, site_id)
        if opted_out:
            await repo.record_whatsapp_opt_out(cur, site_id, lead_id)
            print(f"[MESSAGING] lead {lead_id} opted out of whatsapp")

    return {
        "duplicate": False,
        "opted_out": bool(opted_out),
        "conversation_id": conversation["id"],
        "message_id": message["id"],
        "lead_id": lead_id,
        "window": window_state(conversation),
    }


async def record_status(
    site_id: str, provider_message_id: str, status: str,
    at: Optional[datetime] = None, error: Optional[str] = None,
) -> Optional[dict]:
    """Apply a delivery receipt to an outbound message.

    If the message has no provider id yet — the receipt beat our own record of
    the send — the receipt is parked and applied the moment the id is attached.
    """
    async with transaction() as cur:
        row = await repo.update_message_status(
            cur, site_id, provider_message_id, status=status, at=at, error=error
        )
        if row is None:
            await repo.park_receipt(
                cur, site_id, provider_message_id, status=status, error=error, at=at
            )
        return row


# --------------------------------------------------------------------------- #
# outbound
# --------------------------------------------------------------------------- #

async def record_outbound(
    site_id: str,
    *,
    channel: str,
    contact: str,
    body: Optional[str],
    lead_id: Optional[int] = None,
    status: str = "sent",
    error: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    sender_type: Optional[str] = None,
    message_type: str = "text",
    decision_id: Optional[int] = None,
    sent_message_id: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
) -> dict:
    """Append a message TO a contact. Does not send — callers own transmission.

    Outbound never touches the service window: only the contact writing to you
    reopens it.
    """
    key = normalize_contact(channel, contact)
    at = occurred_at or _utcnow()

    async with transaction() as cur:
        conversation = await repo.get_or_create_conversation(
            cur, site_id, channel=channel, contact=key, lead_id=lead_id
        )
        message = await repo.insert_message(
            cur,
            site_id=site_id,
            conversation_id=conversation["id"],
            lead_id=lead_id or conversation.get("lead_id"),
            direction="out",
            channel=channel,
            body=body,
            message_type=message_type,
            status=status,
            error=error,
            provider_message_id=provider_message_id,
            sender_type=sender_type,
            decision_id=decision_id,
            sent_message_id=sent_message_id,
            occurred_at=at,
        )
        await repo.touch_conversation(
            cur, site_id, conversation["id"], direction="out", at=at, window_hours=WINDOW_HOURS,
        )

    return {
        "conversation_id": conversation["id"],
        "message_id": message["id"] if message else None,
    }


async def finalize_outbound(
    site_id: str, message_id: Optional[int], *,
    ok: bool, detail: Optional[str] = None, provider_message_id: Optional[str] = None,
    sent_message_id: Optional[int] = None,
) -> None:
    """Attach the provider's id and outcome to an already-recorded message.

    Split from the insert so the row exists before transmission: Meta's `sent`
    receipt can land within milliseconds, and a receipt for a row that does not
    exist yet is dropped for good.
    """
    if message_id is None:
        return
    async with transaction() as cur:
        await repo.finalize_message(
            cur, site_id, message_id,
            status="sent" if ok else "failed",
            error=None if ok else detail,
            provider_message_id=provider_message_id,
            sent_message_id=sent_message_id,
        )
        # Claim any receipt that arrived while this row was still anonymous.
        if provider_message_id:
            parked = await repo.take_parked_receipt(cur, site_id, provider_message_id)
            if parked:
                await repo.update_message_status(
                    cur, site_id, provider_message_id,
                    status=parked["status"], at=parked["occurred_at"], error=parked["error"],
                )


async def send_reply(site_id: str, conversation_id: int, text: str) -> dict:
    """Transmit a human reply on an existing thread and record it.

    Refuses when the customer-service window has closed rather than handing Meta
    a message it will reject: a "sent" row that never arrives is worse than an
    error the operator can act on.
    """
    from config.loader import effective_execution_mode
    from execution.stubs import get_sender

    async with transaction() as cur:
        conversation = await repo.get_conversation(cur, site_id, conversation_id)
    if conversation is None:
        return {"ok": False, "error": "not_found"}

    channel = conversation["channel"]
    if channel == WHATSAPP and not window_open(conversation):
        return {"ok": False, "error": "window_closed", "window": window_state(conversation)}

    mode = effective_execution_mode()
    sender = get_sender(channel, mode)
    if sender is None:
        return {"ok": False, "error": "no_sender", "detail": f"no sender for {channel}"}

    # Record before transmitting so a delivery receipt can never arrive before
    # the row it belongs to exists.
    recorded = await record_outbound(
        site_id,
        channel=channel,
        contact=conversation["contact"],
        body=text,
        lead_id=conversation.get("lead_id"),
        status="queued",
        sender_type=sender.sender_type,
    )

    result = await sender.send(conversation["contact"], text, metadata={
        "conversation_id": conversation_id,
        "kind": "human_reply",
        # The window was checked above; without this a template-pinned deployment
        # would replace what the operator typed with a canned template body.
        "window_open": True,
    })

    await finalize_outbound(
        site_id, recorded["message_id"],
        ok=result.ok, detail=result.detail, provider_message_id=result.provider_message_id,
    )
    return {
        "ok": result.ok,
        "detail": result.detail,
        "mode": mode,
        "conversation_id": conversation_id,
        "message_id": recorded["message_id"],
    }


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

async def list_inbox(site_id: str, limit: int = 200) -> list[dict]:
    async with transaction() as cur:
        rows = await repo.list_conversations(cur, site_id, limit)
    now = _utcnow()
    for row in rows:
        row["window"] = window_state(row, now)
    return rows


async def get_thread(site_id: str, conversation_id: int, limit: int = 500) -> Optional[dict]:
    async with transaction() as cur:
        conversation = await repo.get_conversation(cur, site_id, conversation_id)
        if conversation is None:
            return None
        messages = await repo.list_messages(cur, site_id, conversation_id, limit)
        lead = None
        if conversation.get("lead_id"):
            lead = await repo.get_lead_by_id(cur, site_id, conversation["lead_id"])
    conversation["window"] = window_state(conversation)
    return {"conversation": conversation, "lead": lead, "messages": messages}


async def mark_read(site_id: str, conversation_id: int) -> None:
    async with transaction() as cur:
        await repo.mark_conversation_read(cur, site_id, conversation_id)
