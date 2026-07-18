"""Layer 2, Stage B — Gemini classification.

Sends the Stage A feature summary to the model and expects STRICT JSON:
{ funnel_stage, intent_score, likely_objections[], persona_signals{} }.

Robustness (spec: "handle malformed JSON gracefully — retry once, then flag"):
  * The JSON object is extracted from the response text (tolerates stray prose
    or code fences), then validated.
  * On failure the call is retried with a stricter instruction, up to
    scoring.stage_b.retries times; if it still fails the caller flags the lead.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from config.loader import get_config
from llm import gemini_client
from llm.json_utils import extract_json_object
from scoring.schemas import ScoreResult

PROMPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "prompts" / "scoring_stage_b.md"

_SYSTEM = "You are a precise B2B marketing funnel analyst. Follow the output contract exactly."


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_prompt(features: dict[str, Any], stages: list[str]) -> str:
    template = _load_prompt_template()
    features_json = json.dumps(features, indent=2, default=str)
    return (
        template
        .replace("<<STAGES>>", ", ".join(stages))
        .replace("<<FEATURES>>", features_json)
    )


def _validate(data: dict, stages: list[str]) -> tuple[Optional[ScoreResult], Optional[str]]:
    if not isinstance(data, dict):
        return None, "response was not a JSON object"

    stage = data.get("funnel_stage")
    if stage not in stages:
        return None, f"funnel_stage {stage!r} not in {stages}"

    raw_score = data.get("intent_score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        return None, f"intent_score {raw_score!r} is not a number"
    score = max(0, min(100, score))  # clamp to the valid range

    objections = data.get("likely_objections") or []
    if not isinstance(objections, list):
        return None, "likely_objections is not an array"
    objections = [str(o) for o in objections]

    persona = data.get("persona_signals") or {}
    if not isinstance(persona, dict):
        return None, "persona_signals is not an object"

    return ScoreResult(
        funnel_stage=stage,
        intent_score=score,
        likely_objections=objections,
        persona_signals=persona,
    ), None


async def classify(features: dict[str, Any], site_id: str = "default") -> tuple[Optional[ScoreResult], Optional[str]]:
    """Return (ScoreResult, None) on success or (None, error_message) after
    exhausting retries."""
    cfg = get_config("scoring", site_id)
    stages = cfg.get("funnel_stages", ["TOFU", "MOFU", "BOFU"])
    stage_b = cfg.get("stage_b", {})
    max_tokens = int(stage_b.get("max_tokens", 1024))
    retries = int(stage_b.get("retries", 1))

    user = _build_user_prompt(features, stages)
    last_error = "no attempts made"

    for attempt in range(retries + 1):
        try:
            text = await gemini_client.complete_text(_SYSTEM, user, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 — surface API/transport errors as a flag
            last_error = f"{type(exc).__name__}: {exc}"
            break

        data = extract_json_object(text)
        if data is not None:
            result, error = _validate(data, stages)
            if result is not None:
                return result, None
            last_error = error or "validation failed"
        else:
            last_error = "no JSON object found in response"

        # Tighten the instruction and try again.
        user += (
            "\n\nYour previous response could not be parsed as strict JSON. "
            "Respond with ONLY the JSON object — no prose, no code fences."
        )

    return None, last_error
