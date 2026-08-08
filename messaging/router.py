"""Messaging routes.

PUBLIC (Meta calls these — they cannot be behind admin auth):
  GET  /webhooks/whatsapp     one-time verification handshake
  POST /webhooks/whatsapp     inbound messages + delivery receipts

ADMIN (mounted behind HTTP Basic by main.py):
  GET  /api/conversations                 inbox list
  GET  /api/conversations/{id}            one thread
  POST /api/conversations/{id}/read       clear the unread badge
  POST /api/conversations/{id}/reply      send a human reply
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from config.settings import get_settings
from deps import get_site_id
from messaging import meta_webhook, service

public_router = APIRouter(tags=["messaging"])
admin_router = APIRouter(tags=["messaging"])


# --------------------------------------------------------------------------- #
# Meta webhook (public)
# --------------------------------------------------------------------------- #

@public_router.get("/webhooks/whatsapp", include_in_schema=False)
async def verify_webhook(request: Request) -> Response:
    """Meta's one-time handshake: echo hub.challenge if the token matches."""
    params = request.query_params
    token = get_settings().meta_wa_verify_token
    if not token:
        raise HTTPException(status_code=503, detail="META_WA_VERIFY_TOKEN not configured")
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == token:
        # Must be returned as bare text — Meta compares the body verbatim.
        return Response(content=params.get("hub.challenge") or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


# Strong references to in-flight relay tasks: asyncio only holds weak ones, so a
# fire-and-forget task can be garbage-collected mid-request.
_forward_tasks: set = set()


async def _forward(url: str, raw: bytes, headers: dict) -> None:
    """Best-effort relay to a second consumer of the same callback URL."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=raw, headers=headers)
        if resp.status_code // 100 != 2:
            print(f"[messaging] forward to {url} returned HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001 — a downstream consumer must never
        print(f"[messaging] forward to {url} failed: {exc!r}")   # break our ingestion


@public_router.post("/webhooks/whatsapp", include_in_schema=False)
async def receive_webhook(request: Request, site_id: str = Depends(get_site_id)) -> dict:
    """Ingest inbound messages and delivery receipts.

    Always answers 200 once the signature checks out. Meta retries any non-2xx,
    so a message we already stored (or one we cannot parse) must not look like a
    failure or it will be redelivered forever.
    """
    settings = get_settings()
    raw = await request.body()

    if not settings.meta_wa_app_secret:
        # Fail closed: an unsigned public webhook lets anyone inject messages.
        raise HTTPException(status_code=503, detail="META_WA_APP_SECRET not configured")
    if not meta_webhook.verify_signature(
        raw, request.headers.get("x-hub-signature-256"), settings.meta_wa_app_secret
    ):
        raise HTTPException(status_code=403, detail="bad signature")

    try:
        payload = await request.json()
    except Exception:
        # A body we cannot parse is not something a retry will fix. Answering
        # non-2xx would make Meta redeliver it indefinitely.
        return {"status": "ignored", "reason": "unparseable body"}
    parsed = meta_webhook.parse(payload)

    stored = duplicates = 0
    for message in parsed["messages"]:
        if not message.get("contact"):
            continue
        result = await service.record_inbound(
            site_id,
            channel=service.WHATSAPP,
            contact=message["contact"],
            body=message["body"],
            provider_message_id=message["provider_message_id"],
            message_type=message["message_type"],
            media=message["media"],
            occurred_at=message["occurred_at"],
            profile_name=message["profile_name"],
        )
        duplicates += int(bool(result.get("duplicate")))
        stored += int(not result.get("duplicate"))

    receipts = 0
    for status in parsed["statuses"]:
        if not status.get("provider_message_id"):
            continue
        updated = await service.record_status(
            site_id, status["provider_message_id"], status["status"],
            at=status["occurred_at"], error=status["error"],
        )
        receipts += int(updated is not None)

    if settings.meta_wa_forward_url:
        task = asyncio.create_task(_forward(
            settings.meta_wa_forward_url, raw,
            {"Content-Type": "application/json",
             "X-Hub-Signature-256": request.headers.get("x-hub-signature-256", "")},
        ))
        _forward_tasks.add(task)
        task.add_done_callback(_forward_tasks.discard)

    return {"status": "ok", "received": stored, "duplicates": duplicates, "receipts": receipts}


# --------------------------------------------------------------------------- #
# Inbox (admin)
# --------------------------------------------------------------------------- #

@admin_router.get("/api/conversations")
async def api_conversations(site_id: str = Depends(get_site_id)) -> dict:
    conversations = await service.list_inbox(site_id)
    return {
        "site_id": site_id,
        "unread": sum(c["unread_count"] for c in conversations),
        "conversations": conversations,
    }


@admin_router.get("/api/conversations/{conversation_id}")
async def api_thread(conversation_id: int, site_id: str = Depends(get_site_id)) -> dict:
    thread = await service.get_thread(site_id, conversation_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"site_id": site_id, **thread}


@admin_router.post("/api/conversations/{conversation_id}/read")
async def api_mark_read(conversation_id: int, site_id: str = Depends(get_site_id)) -> dict:
    await service.mark_read(site_id, conversation_id)
    return {"status": "ok", "conversation_id": conversation_id}


@admin_router.post("/api/conversations/{conversation_id}/reply")
async def api_reply(
    conversation_id: int, body: dict = Body(...), site_id: str = Depends(get_site_id)
) -> dict:
    """Send a human reply on a thread.

    This is a person choosing to answer someone who wrote in, so it deliberately
    skips the outreach guardrails (rate limit, send window) that govern the
    agent's cold outreach. The 24-hour window still applies — it is Meta's rule,
    not ours, and a text sent outside it is simply rejected.
    """
    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    result = await service.send_reply(site_id, conversation_id, text)
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="conversation not found")
    if result.get("error") == "window_closed":
        raise HTTPException(
            status_code=409,
            detail="the 24-hour WhatsApp window has closed for this contact — "
                   "free-form text will not deliver; use an approved template",
        )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("detail") or "send failed")
    return result
