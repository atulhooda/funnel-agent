#!/usr/bin/env python3
"""Scoring driver (Layer 2).

Triggers the two-stage scoring pass over a running app: materialize profile leads
for every visitor, then run Stage A features + Stage B Gemini classification
for each lead. Mirrors replay.py — a thin HTTP driver.

Usage (after replay.py has ingested journeys, and GEMINI_API_KEY is set):
    python scripts/score.py
    python scripts/score.py --base-url http://localhost:8000 --site-id default
    python scripts/score.py --lead 1          # score just one lead
"""
from __future__ import annotations

import argparse
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run funnel scoring over the app's leads.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--site-id", default="default")
    parser.add_argument("--lead", type=int, default=None, help="Score only this lead_id.")
    args = parser.parse_args(argv)

    headers = {"X-Site-Id": args.site_id}
    # Stage B makes one model call per lead, so allow generous time.
    with httpx.Client(timeout=180.0) as client:
        try:
            client.get(f"{args.base_url}/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach {args.base_url} (is the server running?): {exc}", file=sys.stderr)
            return 1

        try:
            if args.lead is not None:
                resp = client.post(f"{args.base_url}/score/lead/{args.lead}", headers=headers)
                resp.raise_for_status()
                body = resp.json()
                print(f"lead {args.lead}: scored={body.get('scored')} error={body.get('error')}")
                if body.get("result"):
                    r = body["result"]
                    print(
                        f"  -> {r['funnel_stage']} | intent={r['intent_score']} | "
                        f"objections={r['likely_objections']} | persona={r['persona_signals']}"
                    )
            else:
                resp = client.post(f"{args.base_url}/score/run", headers=headers)
                resp.raise_for_status()
                s = resp.json()
                print(
                    "Scoring complete:\n"
                    f"  profiles_created = {s['profiles_created']}\n"
                    f"  leads_scored     = {s['leads_scored']}\n"
                    f"  leads_flagged    = {s['leads_flagged']}\n"
                    f"  total_leads      = {s['total_leads']}"
                )
        except httpx.HTTPStatusError as exc:
            print(f"HTTP {exc.response.status_code} error: {exc.response.text}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
