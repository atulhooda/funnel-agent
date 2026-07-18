"""Layer 3 — decision engine.

Builds a compact context (lead profile + behavior + guardrail constraints +
suggested templates), asks Gemini for a single next action, and parses STRICT
JSON: { action, channel, message, send_at, reasoning }. Malformed JSON -> retry
once, then the caller falls back to handoff_human.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from config.loader import get_config
from decision.schemas import Decision
from llm import gemini_client
from llm.json_utils import extract_json_object

PROMPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "prompts" / "decision.md"

_SYSTEM = (
    "You are a careful B2B growth-marketing decision engine. Follow the output "
    "contract exactly and never propose outreach without the required consent."
)


def _build_context(lead: dict, features: dict, gcfg: dict, templates: dict, now: datetime, recent_outreach: int) -> dict:
    stage = lead.get("funnel_stage")
    max_outreach = int(gcfg.get("rate_limit", {}).get("max_outreach", 2))
    return {
        "lead": {
            "funnel_stage": stage,
            "intent_score": lead.get("intent_score"),
            "likely_objections": lead.get("likely_objections"),
            "persona_signals": lead.get("persona_signals"),
            "identified": bool(lead.get("email") or lead.get("phone")),
        },
        "consent": {
            "email_opt_in": bool(lead.get("email_opt_in")),
            "whatsapp_opt_in": bool(lead.get("whatsapp_opt_in")),
        },
        "contact": {
            "has_email": bool(lead.get("email")),
            "has_whatsapp": bool(lead.get("phone")),
        },
        "behavior": {
            "totals": features.get("totals"),
            "pages_by_lean": features.get("pages_by_lean"),
            "high_intent_events": features.get("high_intent_events"),
            "recency": features.get("recency"),
        },
        "constraints": {
            "now": now.isoformat(),
            "timezone": gcfg.get("timezone", "UTC"),
            "send_window": gcfg.get("send_window"),
            "rate_limit": {
                "max_outreach": max_outreach,
                "window_days": int(gcfg.get("rate_limit", {}).get("window_days", 7)),
                "used": recent_outreach,
                "remaining": max(0, max_outreach - recent_outreach),
            },
        },
        "suggested_templates": {
            "email": (templates.get("email") or {}).get(stage),
            "whatsapp": (templates.get("whatsapp") or {}).get(stage),
        },
    }


def _build_user_prompt(context: dict, actions: list[str]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("<<ACTIONS>>", ", ".join(actions))
        .replace("<<CONTEXT>>", json.dumps(context, indent=2, default=str))
    )


def _validate(data: Any) -> tuple[Optional[Decision], Optional[str]]:
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    try:
        return Decision.model_validate(data), None
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
        return None, f"schema_error: {first.get('loc')} {first.get('msg')}"


async def decide(
    lead: dict,
    features: dict,
    guardrails_cfg: dict,
    templates: dict,
    now: datetime,
    recent_outreach: int,
    site_id: str = "default",
) -> tuple[Optional[Decision], Optional[str], Optional[dict]]:
    """Return (Decision, None, raw) on success or (None, error, raw) after retries."""
    engine_cfg = get_config("decision", site_id).get("engine", {})
    max_tokens = int(engine_cfg.get("max_tokens", 2048))
    retries = int(engine_cfg.get("retries", 1))
    actions = guardrails_cfg.get("allowed_actions", ["send_email", "send_whatsapp", "wait", "handoff_human"])

    context = _build_context(lead, features, guardrails_cfg, templates, now, recent_outreach)
    user = _build_user_prompt(context, actions)
    last_error = "no attempts made"
    last_raw: Optional[dict] = None

    for _attempt in range(retries + 1):
        try:
            text = await gemini_client.complete_text(_SYSTEM, user, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 — API/transport failure -> caller falls back
            last_error = f"{type(exc).__name__}: {exc}"
            break

        data = extract_json_object(text)
        if data is not None:
            last_raw = data
            decision, error = _validate(data)
            if decision is not None:
                return decision, None, data
            last_error = error or "validation failed"
        else:
            last_error = "no JSON object found in response"

        user += (
            "\n\nYour previous response could not be parsed as strict JSON. "
            "Respond with ONLY the JSON object — no prose, no code fences."
        )

    return None, last_error, last_raw
