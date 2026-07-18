"""Layer 3 — guardrails validator. Runs on EVERY decision before acceptance.

All thresholds come from config/guardrails.yaml (nothing hardcoded). Checks:
  * action is allowed; required fields present (per action)
  * outreach channel matches the action
  * consent: email_opt_in / whatsapp_opt_in must be true for the channel
  * rate limit: <= max_outreach accepted outreach actions per rolling window
  * send window: a send's send_at falls within [start_hour, end_hour) local time
Returns a GuardrailResult(passed, violations) that is stored on the decision row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from decision.schemas import Decision, GuardrailResult


def _within_send_window(send_at: datetime, cfg: dict) -> bool:
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    if send_at.tzinfo is None:
        send_at = send_at.replace(tzinfo=timezone.utc)
    local = send_at.astimezone(tz)
    window = cfg.get("send_window", {})
    return int(window.get("start_hour", 0)) <= local.hour < int(window.get("end_hour", 24))


def validate(decision: Decision, lead: dict, recent_outreach: int, now: datetime, cfg: dict) -> GuardrailResult:
    violations: list[str] = []

    allowed = cfg.get("allowed_actions", [])
    outreach_actions = set(cfg.get("outreach_actions", []))
    action = decision.action

    if action not in allowed:
        # Nothing else is meaningful for an unknown action.
        return GuardrailResult(False, [f"unknown_action:{action}"])

    # Required fields (action + reasoning are always required).
    if not (decision.reasoning or "").strip():
        violations.append("missing_field:reasoning")
    for field_name in cfg.get("required_fields", {}).get(action, []):
        value = getattr(decision, field_name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            violations.append(f"missing_field:{field_name}")

    if action in outreach_actions:
        expected_channel = cfg.get("action_channels", {}).get(action)
        if decision.channel != expected_channel:
            violations.append(f"channel_mismatch:expected_{expected_channel}")

        consent_field = cfg.get("channel_consent", {}).get(expected_channel)
        if consent_field and not lead.get(consent_field):
            violations.append(f"consent_denied:{consent_field}")

        if recent_outreach >= int(cfg.get("rate_limit", {}).get("max_outreach", 2)):
            violations.append("rate_limit_exceeded")

        if decision.send_at is not None and not _within_send_window(decision.send_at, cfg):
            violations.append("outside_send_window")

    return GuardrailResult(passed=not violations, violations=violations)
