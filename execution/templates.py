"""Choose which approved WhatsApp template a message should use.

Meta requires a pre-approved template for anything the business starts — a
welcome, a stage nudge — so the agent cannot simply send the copy it composed.
What it CAN do is pick the right approved template and fill its variables.

Names live in config/templates.yaml (editable without a deploy) rather than in
code, so renaming an approved template or adding a stage is a config change.
"""
from __future__ import annotations

from typing import Any, Optional

from config.loader import get_config


def resolve(kind: str, lead: dict, site_id: str = "default") -> Optional[dict]:
    """Return {name, lang, values} for `kind` ("welcome" or a funnel stage).

    None means nothing is configured for it — the caller should fall back to
    plain text, which still delivers if the contact wrote in recently.
    """
    cfg = (get_config("templates", site_id).get("whatsapp_templates") or {})
    spec = cfg.get(kind)
    if not isinstance(spec, dict) or not spec.get("name"):
        return None

    # Fill {{1}}, {{2}}, … in order. A blank would be rejected by Graph, so an
    # unknown field falls back to something addressable rather than an empty
    # string — "Hi ," reads worse than "Hi there,".
    values = []
    for field in spec.get("params") or []:
        value = lead.get(field)
        if field == "name" and not value:
            value = "there"
        values.append(str(value) if value not in (None, "") else "there")

    return {"name": spec["name"], "lang": spec.get("lang") or "en", "values": values}


def for_stage(lead: dict, site_id: str = "default") -> Optional[dict]:
    """Template for whatever stage this lead is in."""
    stage = lead.get("funnel_stage")
    return resolve(stage, lead, site_id) if stage else None
