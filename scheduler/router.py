"""Scheduler control + status (admin-only; wired behind auth in main.py)."""
from __future__ import annotations

from fastapi import APIRouter

from config.settings import get_settings
from scheduler import loop

router = APIRouter(tags=["scheduler"])


@router.post("/scheduler/run-once")
async def run_once() -> dict:
    """Force one pipeline cycle immediately (useful for testing without waiting)."""
    return await loop.run_cycle()


@router.get("/scheduler/status")
async def status() -> dict:
    settings = get_settings()
    return {
        "enabled": settings.scheduler_enabled,
        "interval_seconds": settings.scheduler_interval_seconds,
        "running": loop.is_running(),
        "execution_mode": settings.execution_mode,
        "last_cycle": loop.last_cycle(),
    }
