"""Meta WhatsApp Cloud API webhook — payload parsing and signature checking.

Meta POSTs two kinds of event to the same callback URL:
  * `messages` — someone wrote to your business number
  * `statuses` — a delivery receipt for something you sent

Both arrive inside the same deeply-nested envelope, so this module flattens them
into plain dicts the service layer can consume. Everything here is pure: no DB,
no network, so the parsing can be tested against recorded payloads.

Two provider behaviours drive the design:
  * Meta RETRIES until it gets a 2xx. Handlers must be idempotent — every message
    and status carries a `wamid` we deduplicate on.
  * The signature covers the RAW body. Re-serializing the parsed JSON changes
    byte order and breaks the HMAC, so verification must see the original bytes.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Optional

# Meta delivery receipts, in lifecycle order.
STATUSES = {"sent", "delivered", "read", "failed"}


def verify_signature(raw_body: bytes, header: Optional[str], app_secret: str) -> bool:
    """Check the X-Hub-Signature-256 HMAC over the raw request body."""
    if not app_secret or not header:
        return False
    prefix, _, digest = header.partition("=")
    if prefix != "sha256" or not digest:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def _ts(value: Any) -> Optional[datetime]:
    """Meta sends unix seconds as a string."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _body_and_media(message: dict) -> tuple[Optional[str], dict]:
    """Reduce any message type to display text plus whatever media it carried.

    Non-text messages still need *something* readable in the thread, so each type
    contributes its caption, title or a bracketed placeholder.
    """
    mtype = message.get("type") or "text"

    if mtype == "text":
        return (message.get("text") or {}).get("body"), {}

    if mtype in ("image", "audio", "video", "document", "sticker"):
        payload = message.get(mtype) or {}
        caption = payload.get("caption") or payload.get("filename")
        return (caption or f"[{mtype}]"), {
            "type": mtype,
            "id": payload.get("id"),
            "mime_type": payload.get("mime_type"),
            "filename": payload.get("filename"),
        }

    if mtype == "location":
        loc = message.get("location") or {}
        name = loc.get("name") or loc.get("address") or "[location]"
        return name, {"type": "location", "latitude": loc.get("latitude"),
                      "longitude": loc.get("longitude"), "address": loc.get("address")}

    if mtype == "button":                       # template quick-reply tap
        return (message.get("button") or {}).get("text"), {}

    if mtype == "interactive":                  # list / reply-button selection
        inter = message.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title") or "[interactive reply]", {
            "type": "interactive", "id": reply.get("id"),
        }

    if mtype == "reaction":
        reaction = message.get("reaction") or {}
        return f"[reacted {reaction.get('emoji') or ''}]".strip(), {
            "type": "reaction", "to": reaction.get("message_id"),
        }

    if mtype == "order":
        return "[order]", {"type": "order", "order": message.get("order")}

    return f"[{mtype}]", {"type": mtype}


def parse(payload: dict) -> dict:
    """Flatten a webhook envelope into {messages: [...], statuses: [...]}.

    Unknown shapes are skipped rather than raised: Meta adds event fields over
    time, and a parse error would make us return non-2xx and be retried forever.
    """
    inbound: list[dict] = []
    statuses: list[dict] = []

    if not isinstance(payload, dict):
        return {"messages": inbound, "statuses": statuses}

    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            if not isinstance(value, dict):
                continue

            # wa_id -> profile name, so an inbound message can name its sender.
            profiles = {}
            for contact in value.get("contacts") or []:
                wa_id = (contact or {}).get("wa_id")
                if wa_id:
                    profiles[wa_id] = ((contact or {}).get("profile") or {}).get("name")

            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                sender = message.get("from")
                body, media = _body_and_media(message)
                inbound.append({
                    "provider_message_id": message.get("id"),
                    "contact": sender,
                    "profile_name": profiles.get(sender),
                    "body": body,
                    "message_type": message.get("type") or "text",
                    "media": media,
                    "occurred_at": _ts(message.get("timestamp")),
                    "context": (message.get("context") or {}).get("id"),   # what they replied to
                })

            for status in value.get("statuses") or []:
                if not isinstance(status, dict):
                    continue
                name = status.get("status")
                if name not in STATUSES:
                    continue
                errors = status.get("errors") or []
                detail = None
                if errors:
                    first = errors[0] or {}
                    detail = first.get("title") or first.get("message") or str(first.get("code"))
                statuses.append({
                    "provider_message_id": status.get("id"),
                    "status": name,
                    "contact": status.get("recipient_id"),
                    "occurred_at": _ts(status.get("timestamp")),
                    "error": detail,
                })

    return {"messages": inbound, "statuses": statuses}
