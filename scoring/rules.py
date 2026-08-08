"""Layer 2 — deterministic funnel-stage rules.

Evaluates config/stage_rules.yaml against the engagement rollup. Rules are read
in order and the FIRST match wins, so the strictest rule belongs at the top.

Two jobs:
  * evaluate()    — does any rule fire? Returns the stage it awards, plus the
                    exact evidence, so the dashboard can explain the decision.
  * apply_gates() — when no rule fired, check the LLM's stage against that
                    stage's minimum evidence and downgrade it if unmet.

No stage thresholds are hardcoded here; everything comes from config (file
defaults, overridden live by the Settings page).
"""
from __future__ import annotations

from typing import Any, Optional

from config.loader import get_config

STAGE_ORDER = ["TOFU", "MOFU", "BOFU"]


# --------------------------------------------------------------------------- #
# condition evaluation
# --------------------------------------------------------------------------- #

def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_metrics(cond: dict, metrics: dict) -> bool:
    """Apply every min_*/max_* key in `cond` against the metric bundle.

    A `min_seconds` key tests metrics["seconds"], `min_views` tests
    metrics["views"], and so on — so the same comparison code serves page_type,
    path, section and total conditions.
    """
    for key, raw in cond.items():
        if not (key.startswith("min_") or key.startswith("max_")):
            continue
        want = _num(raw)
        if want is None:
            continue
        metric = key[4:]
        have = _num(metrics.get(metric))
        if have is None:
            return False
        if key.startswith("min_") and have < want:
            return False
        if key.startswith("max_") and have > want:
            return False
    return True


def _describe(cond: dict, metrics: dict) -> str:
    wants = [f"{k[4:]} {'≥' if k.startswith('min_') else '≤'} {v}"
             for k, v in cond.items() if k.startswith(("min_", "max_"))]
    got = ", ".join(f"{k}={v}" for k, v in metrics.items() if _num(v) is not None)
    return f"{' and '.join(wants) or 'present'} (actual: {got or 'none'})"


def _page_type_metrics(features: dict, page_type: str) -> dict:
    slot = (features.get("engagement", {}).get("by_page_type") or {}).get(page_type, {})
    return {
        "seconds": slot.get("seconds", 0),
        "views": slot.get("qualified_views", 0),
        "raw_views": slot.get("views", 0),
        "clicks": slot.get("clicks", 0),
        "scroll_pct": slot.get("max_scroll_pct", 0),
    }


def _path_metrics(features: dict, contains: str) -> dict:
    by_path = features.get("engagement", {}).get("by_path") or {}
    needle = (contains or "").lower()
    total = {"seconds": 0.0, "views": 0, "raw_views": 0, "clicks": 0, "scroll_pct": 0}
    for path, slot in by_path.items():
        if needle and needle not in (path or "").lower():
            continue
        total["seconds"] += slot.get("seconds", 0)
        total["views"] += slot.get("qualified_views", 0)
        total["raw_views"] += slot.get("views", 0)
        total["clicks"] += slot.get("clicks", 0)
        total["scroll_pct"] = max(total["scroll_pct"], slot.get("max_scroll_pct", 0))
    total["seconds"] = round(total["seconds"], 1)
    return total


def _section_metrics(features: dict, name: str) -> dict:
    engagement = features.get("engagement", {})
    sections = engagement.get("sections") or {}
    visits = engagement.get("section_visits") or {}
    needle = (name or "").lower()
    matched = [k for k in sections if not needle or needle in str(k).lower()]
    return {
        "seconds": round(sum(sections[k] for k in matched), 1),
        # visits that actually read the section — not how many sections matched
        "views": sum(visits.get(k, 0) for k in matched),
    }


def _event_metrics(features: dict, cond: dict) -> dict:
    engagement = features.get("engagement", {})
    event_type = cond.get("event_type")
    texts = [str(t).lower() for t in (cond.get("text_contains") or []) if str(t).strip()]

    if texts:
        # Label matching, narrowed to one event type when the rule names one —
        # "a click labelled 'book a call'" must not be satisfied by a form_submit
        # that happens to carry the same text.
        if event_type:
            labels = (engagement.get("labels_by_type") or {}).get(event_type, [])
        else:
            labels = engagement.get("click_labels", [])
        count = sum(1 for label in labels if any(t in label for t in texts))
    elif event_type:
        count = int((engagement.get("events_by_type") or {}).get(event_type, 0))
    else:
        count = sum((engagement.get("events_by_type") or {}).values())
    return {"count": count}


def _total_metrics(features: dict) -> dict:
    engagement = features.get("engagement", {})
    totals = features.get("totals", {})
    return {
        "active_seconds": engagement.get("active_seconds", 0),
        "pageviews": engagement.get("qualified_visits", 0),
        "raw_pageviews": totals.get("pageviews", 0),
        "sessions": engagement.get("sessions", 0),
        "active_days": totals.get("active_days", 0),
        "distinct_pages": engagement.get("distinct_pages", 0),
        "clicks": engagement.get("clicks", 0),
        # How many visits reported any time at all — guard a "they barely stayed"
        # rule with min_measured_visits so it can't fire on missing telemetry.
        "measured_visits": engagement.get("measured_visits", 0),
    }


def _eval_condition(cond: dict, features: dict) -> tuple[bool, str]:
    if not isinstance(cond, dict):
        return False, "invalid condition"
    ctype = cond.get("type") or "total"

    if ctype == "page_type":
        target = cond.get("page_type") or ""
        metrics = _page_type_metrics(features, target)
        label = f"page type '{target}'"
    elif ctype == "path":
        target = cond.get("contains") or cond.get("path") or ""
        metrics = _path_metrics(features, target)
        label = f"path contains '{target}'"
    elif ctype == "section":
        target = cond.get("name") or ""
        metrics = _section_metrics(features, target)
        label = f"section '{target}'"
    elif ctype == "event":
        metrics = _event_metrics(features, cond)
        target = cond.get("event_type") or ", ".join(cond.get("text_contains") or []) or "any"
        label = f"event '{target}'"
    elif ctype == "total":
        metrics = _total_metrics(features)
        label = "overall"
    else:
        return False, f"unknown condition type '{ctype}'"

    ok = _check_metrics(cond, metrics)

    if "identified" in cond:
        ok = ok and (bool(features.get("identified")) == bool(cond["identified"]))
        label += " + identified" if cond["identified"] else " + anonymous"

    return ok, f"{label}: {_describe(cond, metrics)}"


def _eval_group(rule: dict, features: dict) -> tuple[bool, list[str]]:
    """A rule matches when all of `all` hold, at least one of `any` holds, and
    none of `none` hold. Missing groups are skipped; a rule with no conditions
    never matches (guards against an empty rule catching every lead)."""
    when = rule.get("when") or {}
    all_conds = when.get("all") or []
    any_conds = when.get("any") or []
    none_conds = when.get("none") or []
    if not (all_conds or any_conds or none_conds):
        return False, []

    evidence: list[str] = []

    for cond in all_conds:
        ok, why = _eval_condition(cond, features)
        evidence.append(("✓ " if ok else "✗ ") + why)
        if not ok:
            return False, evidence

    if any_conds:
        hits = []
        for cond in any_conds:
            ok, why = _eval_condition(cond, features)
            hits.append(ok)
            evidence.append(("✓ " if ok else "· ") + why)
        if not any(hits):
            return False, evidence

    for cond in none_conds:
        ok, why = _eval_condition(cond, features)
        if ok:
            evidence.append("✗ excluded by: " + why)
            return False, evidence
        evidence.append("✓ not " + why)

    return True, evidence


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def config_for(site_id: str = "default") -> dict:
    return get_config("stage_rules", site_id) or {}


def mode(site_id: str = "default") -> str:
    m = config_for(site_id).get("mode", "rules_first")
    return m if m in ("rules_first", "rules_only", "llm_only") else "rules_first"


def enabled(site_id: str = "default") -> bool:
    cfg = config_for(site_id)
    return bool(cfg.get("enabled", True)) and mode(site_id) != "llm_only"


def evaluate(features: dict, site_id: str = "default") -> Optional[dict]:
    """Return {stage, intent_score, rule, evidence} for the first matching rule,
    or None when nothing matched."""
    if not enabled(site_id):
        return None
    stages = get_config("scoring", site_id).get("funnel_stages", STAGE_ORDER)

    for rule in config_for(site_id).get("rules") or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        stage = rule.get("stage")
        if stage not in stages:
            continue
        matched, evidence = _eval_group(rule, features)
        if matched:
            score = _num(rule.get("intent_score"))
            return {
                "stage": stage,
                "intent_score": int(max(0, min(100, score))) if score is not None else None,
                "rule": rule.get("name") or "unnamed rule",
                "evidence": evidence,
            }
    return None


def _gate_metrics(features: dict) -> dict:
    engagement = features.get("engagement", {})
    metrics = _total_metrics(features)
    by_lean = engagement.get("by_lean") or {}
    metrics["lean_seconds"] = {k: v.get("seconds", 0) for k, v in by_lean.items()}
    metrics["lean_views"] = {k: v.get("views", 0) for k, v in by_lean.items()}
    return metrics


def apply_gates(stage: Optional[str], features: dict, site_id: str = "default") -> tuple[Optional[str], Optional[str]]:
    """Hold a model-assigned stage to its minimum evidence.

    Returns (stage, reason). A stage that fails its gate is downgraded one step
    and keeps being re-checked, so BOFU with no evidence at all lands on TOFU.
    """
    if stage is None or not enabled(site_id):
        return stage, None
    gates = config_for(site_id).get("gates") or {}
    metrics = _gate_metrics(features)
    original = stage
    reasons: list[str] = []
    # Downgrade along the CONFIGURED ladder — hardcoding STAGE_ORDER here would
    # silently disable every gate for a site that renamed its stages.
    order = get_config("scoring", site_id).get("funnel_stages") or STAGE_ORDER

    while stage in gates and stage in order:
        gate = gates.get(stage) or {}
        unmet: list[str] = []

        for key, want_raw in gate.items():
            want = _num(want_raw)
            if key in ("min_lean_seconds", "min_lean_views"):
                metric_map = metrics["lean_seconds" if key.endswith("seconds") else "lean_views"]
                for lean, threshold in (want_raw or {}).items():
                    need = _num(threshold)
                    have = _num(metric_map.get(lean, 0)) or 0
                    if need is not None and have < need:
                        unmet.append(f"{lean} {'seconds' if key.endswith('seconds') else 'views'} "
                                     f"{have} < {need}")
                continue
            if want is None or not key.startswith(("min_", "max_")):
                continue
            have = _num(metrics.get(key[4:]))
            have = 0 if have is None else have
            if key.startswith("min_") and have < want:
                unmet.append(f"{key[4:]} {have} < {want}")
            elif key.startswith("max_") and have > want:
                unmet.append(f"{key[4:]} {have} > {want}")

        if not unmet:
            break
        idx = order.index(stage)
        if idx == 0:
            break
        reasons.append(f"{stage} gate unmet ({'; '.join(unmet)})")
        stage = order[idx - 1]

    if stage != original:
        return stage, f"downgraded {original} → {stage}: " + "; ".join(reasons)
    return stage, None
