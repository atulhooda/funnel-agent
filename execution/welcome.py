"""The welcome message — sent the moment someone registers.

Deliberately not a decision the model makes: it is a fixed response to a thing
the person just did, so it fires straight from identify rather than waiting for
the scheduler's next pass. Everything else the agent sends goes through the
decision engine.

Three refusals, in order:
  * no marketing consent -> nothing (the template is Marketing category, and
    Meta only permits those to contacts who opted in);
  * already welcomed     -> nothing (identify re-runs on every correction and
    re-verification; nobody gets welcomed twice for fixing a typo);
  * no approved template -> nothing (free text cannot open a conversation).

The send-window guardrail is intentionally not applied. That window exists to
stop cold outreach landing at 3am; this is a reply to something the person did
seconds ago, and arriving immediately is the point.
"""
from __future__ import annotations

from datetime import datetime, timezone

from config.loader import effective_execution_mode
from db import repositories as repo
from db.connection import transaction
from execution import templates
from execution.stubs import get_sender
from messaging import service as messaging

CHANNEL = "whatsapp"


async def send_welcome(site_id: str, lead_id: int) -> dict:
    """Send the welcome template to a newly registered lead. Never raises."""
    try:
        async with transaction() as cur:
            lead = await repo.get_lead_by_id(cur, site_id, lead_id)

        if lead is None:
            return {"sent": False, "reason": "lead not found"}
        if not lead.get("whatsapp_opt_in"):
            return {"sent": False, "reason": "no whatsapp consent"}
        if not lead.get("phone"):
            return {"sent": False, "reason": "no phone"}
        if lead.get("welcomed_at"):
            return {"sent": False, "reason": "already welcomed"}

        spec = templates.resolve("welcome", lead, site_id)
        if not spec:
            return {"sent": False, "reason": "no welcome template configured"}

        # Claim the welcome BEFORE sending. Two registrations racing through
        # identify would otherwise both pass the check above and send twice;
        # losing a welcome is better than sending one twice.
        async with transaction() as cur:
            claimed = await repo.claim_welcome(cur, site_id, lead_id)
        if not claimed:
            return {"sent": False, "reason": "already welcomed"}

        mode = effective_execution_mode()
        sender = get_sender(CHANNEL, mode)
        if sender is None:
            return {"sent": False, "reason": "no whatsapp sender"}

        body = spec.get("body") or f"welcome template: {spec['name']}"
        queued = await messaging.record_outbound(
            site_id, channel=CHANNEL, contact=lead["phone"], body=body,
            lead_id=lead_id, status="queued", sender_type=sender.sender_type,
            message_type="template",
        )
        result = await sender.send(lead["phone"], body, metadata={
            "kind": "welcome", "template": spec, "window_open": False,
        })
        await messaging.finalize_outbound(
            site_id, queued["message_id"], ok=result.ok, detail=result.detail,
            provider_message_id=result.provider_message_id,
        )

        async with transaction() as cur:
            await repo.insert_sent_message(
                cur, site_id=site_id, lead_id=lead_id, decision_id=None,
                channel=CHANNEL, sender_type=sender.sender_type,
                to_address=lead["phone"], message=body,
                metadata={"kind": "welcome", "template": spec["name"], "mode": mode,
                          "detail": result.detail},
                status="sent" if result.ok else "skipped",
                skip_reason=None if result.ok else f"provider_error:{result.detail}",
            )

        if not result.ok:
            # Let it be retried: the send failed, so the claim should not stand.
            async with transaction() as cur:
                await repo.release_welcome(cur, site_id, lead_id)

        return {"sent": result.ok, "template": spec["name"], "mode": mode,
                "detail": result.detail}
    except Exception as exc:  # noqa: BLE001 — a welcome must never break identify
        print(f"[WELCOME] lead {lead_id}: {exc!r}")
        return {"sent": False, "reason": repr(exc)}
