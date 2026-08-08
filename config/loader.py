"""Config loader.

Reads the YAML config files in this folder (page_types, scoring, guardrails,
templates) and returns plain dicts to the layers so business rules stay OUT of
code. File config is the source of truth for the prototype; per-site overrides
from the `site_config` DB table can be merged in later without changing callers.
"""
from __future__ import annotations

import copy
import functools
import pathlib
from typing import Optional
from urllib.parse import urlparse

import yaml

CONFIG_DIR = pathlib.Path(__file__).resolve().parent

# In-process config overrides layered on top of the YAML files. Populated from
# the site_config DB table at startup and whenever the Settings page saves, so
# edits take effect without a redeploy. Single Railway replica => one source.
_overrides: dict[str, dict] = {}

# Config blocks whose saved override REPLACES the YAML file instead of merging
# into it. Both are ordered rule lists edited as a whole on the Settings page —
# deep-merging them would resurrect rules the user deleted. Callers that save
# these must send the complete block.
REPLACE_KEYS = {"page_types", "stage_rules"}


@functools.lru_cache(maxsize=None)
def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_config(name: str, site_id: str = "default") -> dict:
    """Return a named config block (e.g. 'page_types'), with any DB override merged in."""
    base = _load_yaml(name)
    override = _overrides.get(name)
    if not override:
        return copy.deepcopy(base)
    if name in REPLACE_KEYS:
        return copy.deepcopy(override)
    return _deep_merge(base, override)


def set_override(name: str, value: Optional[dict]) -> None:
    """Set (or clear) the in-process override for a config block."""
    if value:
        _overrides[name] = value
    else:
        _overrides.pop(name, None)


def all_overrides() -> dict:
    return copy.deepcopy(_overrides)


def merged_override(name: str, partial: dict) -> dict:
    """Deep-merge a partial edit into the existing override for `name` (returns new)."""
    return _deep_merge(_overrides.get(name, {}), partial or {})


def effective_execution_mode() -> str:
    """shadow|live — a saved runtime override wins over the env default."""
    from config.settings import get_settings
    rt = _overrides.get("runtime") or {}
    mode = rt.get("execution_mode")
    return mode if mode in ("shadow", "live") else get_settings().execution_mode


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


def min_seconds_by_page_type(site_id: str = "default") -> dict[str, int]:
    """Return {page_type: qualifying_seconds} — the active time a visit must clear
    before it counts as a real visit to that page type. Page types without an
    explicit `min_seconds` fall back to stage_rules.engagement.min_seconds_per_view."""
    cfg = get_config("page_types", site_id)
    fallback = int(
        (get_config("stage_rules", site_id).get("engagement") or {}).get("min_seconds_per_view", 0)
    )
    out: dict[str, int] = {}
    for rule in cfg.get("rules", []):
        page_type = rule.get("page_type")
        if not page_type:
            continue
        raw = rule.get("min_seconds")
        try:
            out[page_type] = int(raw) if raw is not None else fallback
        except (TypeError, ValueError):
            out[page_type] = fallback
    default_type = cfg.get("default_page_type")
    if default_type and default_type not in out:
        out[default_type] = fallback
    return out


def qualifying_seconds(page_type: Optional[str], site_id: str = "default") -> int:
    """Qualifying dwell for one page type (falls back to the global floor)."""
    fallback = int(
        (get_config("stage_rules", site_id).get("engagement") or {}).get("min_seconds_per_view", 0)
    )
    if not page_type:
        return fallback
    return min_seconds_by_page_type(site_id).get(page_type, fallback)


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
