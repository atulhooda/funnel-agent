#!/usr/bin/env python3
"""Decision driver (Layer 3).

Runs the decision engine + guardrails over a running app's leads: for each lead a
Gemini call proposes {action, channel, message, send_at, reasoning}, guardrails
validate it, and every decision is logged. Mirrors replay.py / score.py.

Usage (after replay.py + score.py, with GEMINI_API_KEY set):
    python scripts/decide.py
    python scripts/decide.py --lead 1
"""
from __future__ import annotations

import argparse
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the decision engine + guardrails over the app's leads.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--site-id", default="default")
    parser.add_argument("--lead", type=int, default=None, help="Decide for only this lead_id.")
    args = parser.parse_args(argv)

    headers = {"X-Site-Id": args.site_id}
    with httpx.Client(timeout=180.0) as client:
        try:
            client.get(f"{args.base_url}/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach {args.base_url} (is the server running?): {exc}", file=sys.stderr)
            return 1

        try:
            if args.lead is not None:
                resp = client.post(f"{args.base_url}/decide/lead/{args.lead}", headers=headers)
                resp.raise_for_status()
                o = resp.json()
                print(f"lead {o['lead_id']}: {o['action']} [{o['status']}]  violations={o['violations']}")
                print(f"  reasoning: {o['reasoning']}")
            else:
                resp = client.post(f"{args.base_url}/decide/run", headers=headers)
                resp.raise_for_status()
                s = resp.json()
                print(
                    "Decisions complete:\n"
                    f"  total_leads = {s['total_leads']}\n"
                    f"  accepted    = {s['accepted']}\n"
                    f"  rejected    = {s['rejected']}\n"
                    f"  by_action   = {s['by_action']}"
                )
        except httpx.HTTPStatusError as exc:
            print(f"HTTP {exc.response.status_code} error: {exc.response.text}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
