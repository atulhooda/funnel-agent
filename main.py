"""FastAPI application entry point.

Wires layer routers and the app lifecycle (DB pool). Run from the repo root:

    uvicorn main:app --reload

All layers wired: 1 tracking, 2 scoring, 3 decision, 4 execution, 5 dashboard
(read-only at /dashboard).
"""
from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from config.settings import get_settings
from dashboard.router import router as dashboard_router
from db.connection import close_pool, open_pool
from decision.router import router as decision_router
from execution.router import router as execution_router
from scoring.router import router as scoring_router
from tracking.router import router as tracking_router

STATIC = pathlib.Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    try:
        yield
    finally:
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

app.include_router(tracking_router)
app.include_router(scoring_router)
app.include_router(decision_router)
app.include_router(execution_router)
app.include_router(dashboard_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/track.js", include_in_schema=False)
async def track_js() -> FileResponse:
    """The browser tracking snippet — add to your site with a <script src> tag."""
    return FileResponse(STATIC / "track.js", media_type="application/javascript")


@app.get("/demo", include_in_schema=False)
async def demo() -> FileResponse:
    """A sample page that loads the snippet — for testing ingestion end to end."""
    return FileResponse(STATIC / "demo.html", media_type="text/html")
