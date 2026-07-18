"""Shared Gemini client — DIRECT Google GenAI SDK calls (no LangChain).

Used by scoring (Stage B) and the decision engine (Layer 3). Sends a prompt and
returns the response text. JSON parsing / retry / flag logic lives in the callers
so each layer can apply its own schema — this module just does the transport.

Config choices for these bounded, structured calls:
  * response_mime_type="application/json" constrains the model to emit JSON.
  * thinking is disabled (budget 0) to keep latency/cost low; callers still
    extract the JSON object defensively in case of any stray text.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from google import genai
from google.genai import types

from config.settings import get_settings


@lru_cache
def get_client() -> genai.Client:
    # Pass None (not "") when unset so the SDK resolves GEMINI_API_KEY /
    # GOOGLE_API_KEY from the environment itself.
    api_key = get_settings().gemini_api_key or None
    return genai.Client(api_key=api_key)


async def complete_text(system: str, user: str, *, max_tokens: int, model: Optional[str] = None) -> str:
    """Send a single-turn request and return the response text (JSON string)."""
    settings = get_settings()
    response = await get_client().aio.models.generate_content(
        model=model or settings.gemini_model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    try:
        return response.text or ""
    except Exception:  # noqa: BLE001 — blocked/empty response -> caller flags it
        return ""
