"""Layer 5 — read-only dashboard.

JSON routes plus one minimal HTML page (which consumes those JSON routes):
  GET /dashboard            the HTML page
  GET /api/overview         summary counts
  GET /api/leads            all leads: stage, intent, consent status
  GET /api/decisions        the decision log with reasoning + guardrail result
  GET /api/sent-messages    the stubbed sent-messages log
Nothing here writes.
"""
from __future__ import annotations

import pathlib
from collections import Counter

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from db import repositories as repo
from db.connection import transaction
from deps import get_site_id

router = APIRouter(tags=["dashboard"])
TEMPLATE = pathlib.Path(__file__).resolve().parent / "templates" / "dashboard.html"


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))


@router.get("/api/leads")
async def api_leads(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "leads": await repo.list_leads(cur, site_id)}


@router.get("/api/decisions")
async def api_decisions(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "decisions": await repo.list_decisions(cur, site_id)}


@router.get("/api/sent-messages")
async def api_sent_messages(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        return {"site_id": site_id, "sent_messages": await repo.list_sent_messages(cur, site_id)}


@router.get("/api/overview")
async def api_overview(site_id: str = Depends(get_site_id)) -> dict:
    async with transaction() as cur:
        leads = await repo.list_leads(cur, site_id)
        decisions = await repo.list_decisions(cur, site_id)
        sent = await repo.list_sent_messages(cur, site_id)
    return {
        "site_id": site_id,
        "overview": {
            "leads": len(leads),
            "by_stage": dict(Counter((lead["funnel_stage"] or "unscored") for lead in leads)),
            "decisions": len(decisions),
            "decisions_by_status": dict(Counter(d["status"] for d in decisions)),
            "decisions_by_action": dict(Counter(d["action"] for d in decisions)),
            "sent": sum(1 for s in sent if s["status"] == "sent"),
            "skipped": sum(1 for s in sent if s["status"] == "skipped"),
        },
    }
