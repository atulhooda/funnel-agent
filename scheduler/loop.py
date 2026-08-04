"""Autonomous pipeline scheduler.

Runs the whole brain on a fixed interval so live browsing traffic becomes scored
visitors, decisions, and (shadow) executions with no manual trigger. Each cycle:

    materialize new profiles -> score only new/changed leads
      -> decide only re-scored leads -> execute accepted outreach (shadow, idempotent)

Only new/changed leads are scored/decided, so Gemini spend and the decision log
stay bounded no matter how often the loop runs. A lock prevents overlapping
cycles (the background loop and a manual /scheduler/run-once can't collide).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from config.settings import get_settings
from decision.service import decide_pending
from execution.service import execute_pending
from scoring.service import score_pending

_MIN_INTERVAL = 15  # floor so a misconfigured interval can't hot-loop

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_last_cycle: Optional[dict] = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_cycle(site_id: Optional[str] = None) -> dict:
    """Run one full pipeline pass. Returns a summary; skips if one is in flight."""
    global _last_cycle
    settings = get_settings()
    site_id = site_id or settings.site_id

    if _lock.locked():
        return {"skipped": "cycle already running"}

    async with _lock:
        scored = await score_pending(site_id)
        decided = await decide_pending(site_id)
        executed = await execute_pending(site_id)
        _last_cycle = {
            "at": _utcnow_iso(),
            "site_id": site_id,
            "scored": scored,
            "decided": decided,
            "executed": executed,
        }
        return _last_cycle


def last_cycle() -> Optional[dict]:
    return _last_cycle


def is_running() -> bool:
    return _task is not None and not _task.done()


async def _loop() -> None:
    interval = max(_MIN_INTERVAL, int(get_settings().scheduler_interval_seconds))
    await asyncio.sleep(5)  # let startup + first healthcheck settle
    while True:
        try:
            summary = await run_cycle()
            print(f"[SCHEDULER] cycle: {summary}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad cycle must never kill the loop
            print(f"[SCHEDULER] cycle error: {exc!r}")
        await asyncio.sleep(interval)


def start() -> None:
    """Start the background loop (no-op if disabled or already running)."""
    global _task
    settings = get_settings()
    if not settings.scheduler_enabled:
        print("[SCHEDULER] disabled (SCHEDULER_ENABLED=false)")
        return
    if is_running():
        return
    _task = asyncio.create_task(_loop())
    print(f"[SCHEDULER] started (every {settings.scheduler_interval_seconds}s)")


async def stop() -> None:
    """Cancel the background loop cleanly on shutdown."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
