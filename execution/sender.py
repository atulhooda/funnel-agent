"""Layer 4 — abstract Sender interface.

Every outbound channel implements `send(to, message, metadata)`. Real providers
(Postmark/SendGrid for email, Twilio/WATI for WhatsApp) drop in behind this exact
interface later — the execution service and DB logging never change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    provider_message_id: Optional[str] = None


class Sender(ABC):
    channel: str = ""        # "email" | "whatsapp"
    sender_type: str = ""    # "stub_email" | "stub_whatsapp" (or a real provider id later)

    @abstractmethod
    async def send(self, to: str, message: str, metadata: dict) -> SendResult:
        """Transmit the message. Implementations MUST NOT persist to the DB —
        the execution service records every send/skip to `sent_messages`."""
        raise NotImplementedError
