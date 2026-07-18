"""Config loader.

Reads the YAML config files in this folder (page_types, scoring, guardrails,
templates) and returns plain dicts to the layers so business rules stay OUT of
code. File config is the source of truth for the prototype; per-site overrides
from the `site_config` DB table can be merged in later without changing callers.
"""
from __future__ import annotations

import functools
import pathlib
from typing import Optional
from urllib.parse import urlparse

import yaml

CONFIG_DIR = pathlib.Path(__file__).resolve().parent


@functools.lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def get_config(name: str, site_id: str = "default") -> dict:
    """Return a named config block (e.g. 'page_types').

    `site_id` is accepted now so per-site DB overrides can be layered on later
    without touching any caller.
    """
    return _load_yaml(name)


# --- page-type classification (config-driven; Layer 2 reads the same rules) ---

def _normalize_path(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    path = urlparse(url).path or "/"
    return path.lower().rstrip("/") or "/"


def _token_matches(token: str, path: str) -> bool:
    token = token.lower()
    if token == "/":            # root matches only the exact root path
        return path == "/"
    return token in path


def resolve_page_type(url: Optional[str], site_id: str = "default") -> tuple[Optional[str], Optional[str]]:
    """Map a URL to (page_type, funnel_lean) using config/page_types.yaml.

    First matching rule wins. Returns the configured defaults when nothing
    matches, or (None, None) when there is no URL to classify.
    """
    path = _normalize_path(url)
    if path is None:
        return (None, None)
    cfg = get_config("page_types", site_id)
    for rule in cfg.get("rules", []):
        if any(_token_matches(t, path) for t in rule.get("match_any", [])):
            return (rule.get("page_type"), rule.get("lean"))
    return (cfg.get("default_page_type"), cfg.get("default_lean"))


def page_type_leans(site_id: str = "default") -> dict[str, Optional[str]]:
    """Return {page_type: funnel_lean} from config — used by scoring Stage A to
    roll page-type counts up into TOFU/MOFU/BOFU buckets."""
    cfg = get_config("page_types", site_id)
    leans: dict[str, Optional[str]] = {}
    for rule in cfg.get("rules", []):
        page_type = rule.get("page_type")
        if page_type:
            leans[page_type] = rule.get("lean")
    default_type = cfg.get("default_page_type")
    if default_type and default_type not in leans:
        leans[default_type] = cfg.get("default_lean")
    return leans
