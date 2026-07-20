#!/usr/bin/env python3
"""Event-replay script (Layer 1 test harness).

Feeds realistic visitor journeys from scripts/journeys.json into a running
app's POST /track and POST /identify endpoints. No browser snippet needed.

Seeded journeys (see journeys.json):
  (a) TOFU browser  — one blog post, then leaves
  (b) MOFU comparer — multiple visits, testimonials + FAQ + comparison
  (c) BOFU lead     — pricing x2, checkout start, abandon, then identifies

Usage:
    python scripts/replay.py                         # replay all into localhost:8000
    python scripts/replay.py --only bofu_lead        # a single journey by key
    python scripts/replay.py --base-url http://host:8000 --site-id default
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DEFAULT_JOURNEYS = Path(__file__).resolve().parent / "journeys.json"


def load_journeys(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("journeys", [])


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def replay_journey(client: httpx.Client, base_url: str, site_id: str, journey: dict, now: datetime, write_key: str = "") -> int:
    headers = {"X-Site-Id": site_id}
    if write_key:
        headers["X-Write-Key"] = write_key
    anon = journey["anonymous_id"]
    print(f"\n=== {journey.get('label', journey.get('key'))}  [anonymous_id={anon}] ===")

    n_events = 0
    for ev in journey.get("events", []):
        occurred = now - timedelta(minutes=float(ev.get("minutes_ago", 0)))
        payload = {
            "event_type": ev["event_type"],
            "url": ev.get("url"),
            "timestamp": _iso(occurred),
            "anonymous_id": anon,
            "session_id": ev.get("session_id"),
            "metadata": ev.get("metadata") or {},
        }
        resp = client.post(f"{base_url}/track", json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        n_events += 1
        url = ev.get("url") or "-"
        print(
            f"  track    {occurred:%Y-%m-%d %H:%M}  {ev['event_type']:<16} {url:<48} "
            f"-> page_type={body.get('page_type')} event_id={body.get('event_id')}"
        )

    ident = journey.get("identify")
    if ident:
        consent = now - timedelta(minutes=float(ident.get("minutes_ago", 0)))
        payload = {
            "anonymous_id": anon,
            "email": ident.get("email"),
            "phone": ident.get("phone"),
            "email_opt_in": ident.get("email_opt_in", False),
            "whatsapp_opt_in": ident.get("whatsapp_opt_in", False),
            "consent_timestamp": _iso(consent),
            "consent_source": ident.get("consent_source"),
        }
        resp = client.post(f"{base_url}/identify", json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        print(
            f"  identify -> lead_id={body.get('lead_id')} created={body.get('created')} "
            f"backfilled_events={body.get('backfilled_events')}"
        )
    else:
        print("  (stays anonymous — no identify)")

    return n_events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay seeded visitor journeys into the funnel agent API.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--journeys", type=Path, default=DEFAULT_JOURNEYS)
    parser.add_argument("--site-id", default="default")
    parser.add_argument("--only", default=None, help="Replay only the journey with this key.")
    parser.add_argument("--write-key", default=os.environ.get("TRACK_WRITE_KEY", ""),
                        help="X-Write-Key header (defaults to $TRACK_WRITE_KEY).")
    args = parser.parse_args(argv)

    journeys = load_journeys(args.journeys)
    if args.only:
        journeys = [j for j in journeys if j.get("key") == args.only]
        if not journeys:
            print(f"No journey with key={args.only!r} in {args.journeys}", file=sys.stderr)
            return 2

    now = datetime.now(timezone.utc)
    total_events = 0
    try:
        with httpx.Client(timeout=10.0) as client:
            try:
                client.get(f"{args.base_url}/health").raise_for_status()
            except Exception as exc:  # noqa: BLE001 — surface any connection problem plainly
                print(f"Cannot reach {args.base_url} (is the server running?): {exc}", file=sys.stderr)
                return 1
            for journey in journeys:
                total_events += replay_journey(client, args.base_url, args.site_id, journey, now, args.write_key)
    except httpx.HTTPStatusError as exc:
        print(f"HTTP {exc.response.status_code} error: {exc.response.text}", file=sys.stderr)
        return 1

    print(f"\nDone. Replayed {len(journeys)} journey(s), {total_events} event(s) into {args.base_url}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
