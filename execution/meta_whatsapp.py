"""Real WhatsApp sender — Meta WhatsApp Cloud API (Graph API).

Implements the same Sender interface as the stubs, so it drops into the live
registry with no other code changes. It is only used when EXECUTION_MODE=live
AND the token + phone number id are configured; otherwise the stub is used.

What Meta requires (you provide these as env vars):
  * A Meta app with the WhatsApp product, a WhatsApp Business Account (WABA),
    and a phone number → its **Phone Number ID** (META_WA_PHONE_NUMBER_ID).
  * A **System User permanent access token** with `whatsapp_business_messaging`
    (temporary tokens expire in ~24h) → META_WA_ACCESS_TOKEN.
  * For business-initiated outreach (a cold lead), WhatsApp requires an
    **approved message template** — free-form `text` only delivers inside the
    24-hour customer-service window. Set META_WA_MESSAGE_TYPE=template and
    META_WA_TEMPLATE_NAME to an approved template.
  * The recipient must have **opted in** — the funnel guardrails already refuse
    to send without `whatsapp_opt_in`, and execution re-checks it at send time.

Smoke test: every WABA has the default approved template `hello_world`
(no variables) — set message_type=template, template_name=hello_world,
template_body_param=false, and send to a number on your test allow-list.
"""
from __future__ import annotations

import re

import httpx

from config.settings import get_settings
from execution.sender import SendResult, Sender


class MetaWhatsAppSender(Sender):
    channel = "whatsapp"
    sender_type = "meta_whatsapp_cloud"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # transport is injectable so the request logic can be tested without network.
        self._transport = transport

    def _payload(self, to: str, message: str, s) -> dict:
        if s.meta_wa_message_type == "template":
            template: dict = {"name": s.meta_wa_template_name, "language": {"code": s.meta_wa_template_lang}}
            if s.meta_wa_template_body_param:
                template["components"] = [
                    {"type": "body", "parameters": [{"type": "text", "text": message}]}
                ]
            return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}
        # text (only delivers inside the 24h customer-service window)
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }

    async def send(self, to: str, message: str, metadata: dict) -> SendResult:
        s = get_settings()
        to_digits = re.sub(r"\D", "", to or "")   # E.164 digits only, no '+'
        if not to_digits:
            return SendResult(ok=False, detail="no recipient number")

        url = f"https://graph.facebook.com/{s.meta_wa_api_version}/{s.meta_wa_phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {s.meta_wa_access_token}", "Content-Type": "application/json"}
        payload = self._payload(to_digits, message, s)

        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
                resp = await client.post(url, json=payload, headers=headers)
            data = resp.json() if resp.content else {}
        except Exception as exc:  # noqa: BLE001 — network/transport failure
            return SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}")

        if resp.status_code // 100 == 2 and data.get("messages"):
            return SendResult(ok=True, detail="meta whatsapp accepted", provider_message_id=data["messages"][0].get("id"))
        err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        return SendResult(ok=False, detail=f"meta error: {err}")
