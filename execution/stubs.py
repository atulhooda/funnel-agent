"""Layer 4 — StubEmailSender and StubWhatsAppSender.

They print the payload to the console and return success — but transmit NOTHING.
The execution service is what writes the full payload to `sent_messages`. To go
live, swap the instances in the registry below for real providers implementing
the same Sender interface — no other code changes.
"""
from __future__ import annotations

from typing import Optional

from execution.sender import SendResult, Sender


def _preview(message: Optional[str]) -> str:
    return (message or "").strip().replace("\n", " ")[:120]


class StubEmailSender(Sender):
    channel = "email"
    sender_type = "stub_email"

    async def send(self, to: str, message: str, metadata: dict) -> SendResult:
        print(f"[STUB EMAIL] would send to {to!r}: {_preview(message)!r}  (NOT transmitted)")
        return SendResult(ok=True, detail="stub_email: logged only, not transmitted")


class StubWhatsAppSender(Sender):
    channel = "whatsapp"
    sender_type = "stub_whatsapp"

    async def send(self, to: str, message: str, metadata: dict) -> SendResult:
        print(f"[STUB WHATSAPP] would send to {to!r}: {_preview(message)!r}  (NOT transmitted)")
        return SendResult(ok=True, detail="stub_whatsapp: logged only, not transmitted")


# _STUB — always logs, never transmits; used in shadow mode.
_STUB: dict[str, Sender] = {
    "email": StubEmailSender(),
    "whatsapp": StubWhatsAppSender(),
}

_live_cache: Optional[dict[str, Sender]] = None


def _live_registry() -> dict[str, Sender]:
    """Live senders. Real providers are swapped in based on config; any channel
    not configured falls back to its stub — so 'live' mode is safe by default.
    (Add an email provider for 'email' the same way.)"""
    global _live_cache
    if _live_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        registry: dict[str, Sender] = dict(_STUB)
        if settings.meta_wa_access_token and settings.meta_wa_phone_number_id:
            from execution.meta_whatsapp import MetaWhatsAppSender

            registry["whatsapp"] = MetaWhatsAppSender()
        _live_cache = registry
    return _live_cache


def get_sender(channel: str, mode: str = "shadow") -> Optional[Sender]:
    return (_live_registry() if mode == "live" else _STUB).get(channel)
