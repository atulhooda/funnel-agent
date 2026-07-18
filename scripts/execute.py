#!/usr/bin/env python3
"""Execution driver (Layer 4).

Executes accepted outreach decisions via the STUB senders (email / WhatsApp).
Consent is re-checked at send time; sends and skips are logged to sent_messages.
Nothing is transmitted. Mirrors replay.py / score.py / decide.py.

Usage (after decide.py):
    python scripts/execute.py
    python scripts/execute.py --decision 1
"""
from __future__ import annotations

import argparse
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute accepted outreach decisions (stubbed).")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--site-id", default="default")
    parser.add_argument("--decision", type=int, default=None, help="Execute only this decision_id.")
    args = parser.parse_args(argv)

    headers = {"X-Site-Id": args.site_id}
    with httpx.Client(timeout=60.0) as client:
        try:
            client.get(f"{args.base_url}/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach {args.base_url} (is the server running?): {exc}", file=sys.stderr)
            return 1

        try:
            if args.decision is not None:
                resp = client.post(f"{args.base_url}/execute/decision/{args.decision}", headers=headers)
                resp.raise_for_status()
                print(resp.json())
            else:
                resp = client.post(f"{args.base_url}/execute/run", headers=headers)
                resp.raise_for_status()
                s = resp.json()
                print(
                    "Execution complete (stubbed — nothing transmitted):\n"
                    f"  pending = {s['pending']}\n"
                    f"  sent    = {s['sent']}\n"
                    f"  skipped = {s['skipped']}"
                )
        except httpx.HTTPStatusError as exc:
            print(f"HTTP {exc.response.status_code} error: {exc.response.text}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
