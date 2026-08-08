"""Layer 4 orchestration.

Executes accepted outreach decisions through the stub senders. Critically, it
RE-CHECKS consent at send time (consent can change after the decision was made):
no opt-in -> no send, logged as a 'skipped' row with a reason. Every send and
every skip is recorded in `sent_messages`; nothing is transmitted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from config.loader import effective_execution_mode, get_config
from config.settings import get_settings
from db import repositories as repo
from db.connection import transaction
from execution.stubs import get_sender
from messaging import service as messaging

# channel -> the leads column holding that channel's address
CHANNEL_CONTACT = {"email": "email", "whatsapp": "phone"}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


async def execute_decision(site_id: str, decision_id: int) -> Optional[dict]:
    gcfg = get_config("guardrails", site_id)
    channel_consent = gcfg.get("channel_consent", {})
    outreach_actions = set(gcfg.get("outreach_actions", []))

    async with transaction() as cur:
        decision = await repo.get_decision_by_id(cur, site_id, decision_id)
        if decision is None:
            return None
        if await repo.sent_message_exists_for_decision(cur, site_id, decision_id):
            return {"decision_id": decision_id, "status": "already_executed"}
        lead = await repo.get_lead_by_id(cur, site_id, decision["lead_id"])

    # Only accepted outreach is executable.
    if decision["status"] != "accepted" or decision["action"] not in outreach_actions:
        return {
            "decision_id": decision_id,
            "status": "not_executable",
            "reason": f"{decision['action']}/{decision['status']}",
        }

    channel = decision["channel"]
    consent_field = channel_consent.get(channel)
    contact_field = CHANNEL_CONTACT.get(channel)
    to_address = lead.get(contact_field) if (lead and contact_field) else None
    mode = effective_execution_mode()
    sender = get_sender(channel, mode)

    # --- consent re-check at SEND time (defense in depth beyond the guardrail) ---
    skip_reason: Optional[str] = None
    if not (consent_field and lead and lead.get(consent_field)):
        skip_reason = f"consent_not_granted:{consent_field}"
    elif not to_address:
        skip_reason = "no_contact_address"
    elif sender is None:
        skip_reason = f"no_sender_for_channel:{channel}"

    if skip_reason:
        async with transaction() as cur:
            row = await repo.insert_sent_message(
                cur,
                site_id=site_id,
                lead_id=decision["lead_id"],
                decision_id=decision_id,
                channel=channel,
                sender_type=sender.sender_type if sender else None,
                to_address=to_address,
                message=decision["message"],
                metadata={"decision_send_at": _iso(decision["send_at"])},
                status="skipped",
                skip_reason=skip_reason,
            )
        print(f"[EXECUTION] SKIP decision {decision_id} ({channel}) -> {skip_reason}")
        return {"decision_id": decision_id, "status": "skipped", "skip_reason": skip_reason, "sent_message_id": row["id"]}

    # --- dispatch ---
    # Thread bookkeeping is a read-side convenience wrapped around the send. It
    # must never be able to stop one: if any of it fails we log and transmit
    # anyway, because `sent_messages` below is the record that actually matters.
    conversation = None
    queued: dict = {}
    try:
        # Open (or find) the thread first so the sender knows whether this
        # contact's 24-hour window is open, and so agent sends and human replies
        # land in one conversation rather than two parallel histories.
        conversation = await messaging.ensure_conversation(
            site_id, channel=channel, contact=to_address, lead_id=decision["lead_id"]
        )
        # Record the message BEFORE transmitting: a delivery receipt for a row
        # that does not exist yet is dropped, and Meta's first receipt can arrive
        # within milliseconds of the send returning.
        queued = await messaging.record_outbound(
            site_id,
            channel=channel,
            contact=to_address,
            body=decision["message"],
            lead_id=decision["lead_id"],
            status="queued",
            sender_type=sender.sender_type,
            decision_id=decision_id,
        )
    except Exception as exc:  # noqa: BLE001 — never block an outreach send
        print(f"[EXECUTION] decision {decision_id}: thread bookkeeping failed, sending anyway: {exc!r}")

    result = await sender.send(to_address, decision["message"], metadata={
        "decision_id": decision_id,
        "conversation_id": (conversation or {}).get("id"),
        # No thread means no window evidence; assume open so the configured
        # message type decides, exactly as it did before conversations existed.
        "window_open": messaging.window_open(conversation) if conversation else True,
    })
    status = "sent" if result.ok else "skipped"
    reason = None if result.ok else f"provider_error:{result.detail}"

    async with transaction() as cur:
        row = await repo.insert_sent_message(
            cur,
            site_id=site_id,
            lead_id=decision["lead_id"],
            decision_id=decision_id,
            channel=channel,
            sender_type=sender.sender_type,
            to_address=to_address,
            message=decision["message"],
            metadata={
                "decision_send_at": _iso(decision["send_at"]),
                "detail": result.detail,
                "provider_message_id": result.provider_message_id,
                "mode": mode,
                "note": "stubbed — not transmitted" if mode != "live" else "live send",
            },
            status=status,
            skip_reason=reason,
        )

    # `sent_messages` is the decision's audit trail and is already written; the
    # thread is a read-side convenience, so a failure updating it must not abort
    # execute_pending — the decision would then be stuck as 'already_executed'.
    try:
        await messaging.finalize_outbound(
            site_id, queued.get("message_id"),
            ok=result.ok, detail=result.detail,
            provider_message_id=result.provider_message_id,
            sent_message_id=row["id"],
        )
    except Exception as exc:  # noqa: BLE001 — thread bookkeeping is not the send
        print(f"[EXECUTION] decision {decision_id} sent, but thread update failed: {exc!r}")

    return {"decision_id": decision_id, "status": status, "to": to_address,
            "sent_message_id": row["id"], "conversation_id": (conversation or {}).get("id")}


async def execute_pending(site_id: str) -> dict:
    gcfg = get_config("guardrails", site_id)
    outreach = gcfg.get("outreach_actions", [])
    async with transaction() as cur:
        decision_ids = await repo.list_pending_outreach_decisions(cur, site_id, outreach)

    sent = skipped = 0
    for decision_id in decision_ids:
        outcome = await execute_decision(site_id, decision_id)
        if outcome and outcome.get("status") == "sent":
            sent += 1
        elif outcome and outcome.get("status") == "skipped":
            skipped += 1

    return {"site_id": site_id, "pending": len(decision_ids), "sent": sent, "skipped": skipped}
