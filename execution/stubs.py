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


# Swap-point for real providers — keyed by channel. Replace a value with e.g.
# PostmarkSender()/TwilioSender() and nothing else in the app changes.
_REGISTRY: dict[str, Sender] = {
    "email": StubEmailSender(),
    "whatsapp": StubWhatsAppSender(),
}


def get_sender(channel: str) -> Optional[Sender]:
    return _REGISTRY.get(channel)
