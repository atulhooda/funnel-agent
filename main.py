"""FastAPI application entry point.

Wires layer routers and the app lifecycle (DB pool). Run from the repo root:

    uvicorn main:app --reload

All layers wired: 1 tracking, 2 scoring, 3 decision, 4 execution, 5 dashboard
(read-only at /dashboard).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from dashboard.router import router as dashboard_router
from db.connection import close_pool, open_pool
from decision.router import router as decision_router
from execution.router import router as execution_router
from scoring.router import router as scoring_router
from tracking.router import router as tracking_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Behavioral Funnel Agent", version="0.1.0", lifespan=lifespan)

app.include_router(tracking_router)
app.include_router(scoring_router)
app.include_router(decision_router)
app.include_router(execution_router)
app.include_router(dashboard_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}
