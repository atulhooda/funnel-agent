"""FastAPI application entry point.

Wires layer routers and the app lifecycle (DB pool). Run from the repo root:

    uvicorn main:app --reload

All layers wired: 1 tracking, 2 scoring, 3 decision, 4 execution, 5 dashboard
(read-only at /dashboard).
"""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from config import loader
from config.settings import get_settings
from dashboard.router import router as dashboard_router
from db import repositories as repo
from db.connection import close_pool, ensure_schema, open_pool, transaction
from decision.router import router as decision_router
from deps import require_admin
from execution.router import router as execution_router
from scheduler import loop as scheduler
from scheduler.router import router as scheduler_router
from scoring.router import router as scoring_router
from tracking.router import router as tracking_router

STATIC = pathlib.Path(__file__).resolve().parent / "static"


async def _load_config_overrides() -> None:
    """Read saved config overrides from site_config into the in-process loader."""
    try:
        async with transaction() as cur:
            rows = await repo.get_site_config(cur, get_settings().site_id)
        for key, value in rows.items():
            loader.set_override(key, value)
    except Exception as exc:  # never block startup on config load
        print(f"[config] could not load overrides: {exc!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply the schema first (also waits for the DB to come up), then open the pool.
    if get_settings().run_schema_on_startup:
        await ensure_schema()
    await open_pool()
    await _load_config_overrides()  # apply saved Settings edits over the file config
    scheduler.start()  # autonomous score -> decide -> execute loop (if enabled)
    try:
        yield
    finally:
        await scheduler.stop()
        await close_pool()


app = FastAPI(title="Behavioral Funnel Agent", version="0.1.0", lifespan=lifespan)

# CORS so a browser snippet on your site can POST to /track & /identify.
_origins = [o.strip() for o in get_settings().cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Site-Id", "X-Write-Key"],
    allow_credentials=False,
)

# Public (browser ingestion, guarded by its own write-key): tracking only.
app.include_router(tracking_router)

# Admin-only: the control routes and the dashboard sit behind HTTP Basic auth.
_admin = [Depends(require_admin)]
app.include_router(scoring_router, dependencies=_admin)
app.include_router(decision_router, dependencies=_admin)
app.include_router(execution_router, dependencies=_admin)
app.include_router(scheduler_router, dependencies=_admin)
app.include_router(dashboard_router, dependencies=_admin)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/track.js", include_in_schema=False)
async def track_js() -> FileResponse:
    """The browser tracking snippet — add to your site with a <script src> tag.

    Cached for five minutes only. Without an explicit header the browser picks a
    heuristic lifetime, and a stale snippet keeps reporting page views with no
    engagement data — visitors who look like they never stayed anywhere.
    """
    return FileResponse(
        STATIC / "track.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/world.js", include_in_schema=False)
async def world_js() -> FileResponse:
    """Equirectangular world land path (window.WORLD_LAND) for the live map page."""
    return FileResponse(
        STATIC / "world.js",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/demo", include_in_schema=False)
async def demo() -> FileResponse:
    """A sample page that loads the snippet — for testing ingestion end to end."""
    return FileResponse(STATIC / "demo.html", media_type="text/html")
