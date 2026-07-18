"""Shared helper: pull a JSON object out of a model response.

Tolerates markdown fences or surrounding prose so callers can parse strict-JSON
responses defensively. Used by scoring (Stage B) and the decision engine.
"""
from __future__ import annotations

import json
import re
from typing import Optional


def extract_json_object(text: str) -> Optional[dict]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
