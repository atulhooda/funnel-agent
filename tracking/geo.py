"""Best-effort IP → geo (country/region/city).

Uses a free, keyless HTTPS endpoint (ipwho.is). Called in the background from
ingestion so it never slows /track, and the raw IP is used transiently and never
stored. Any failure is swallowed — geo is a nice-to-have, not required.
"""
from __future__ import annotations

import ipaddress
from typing import Optional

import httpx


def client_ip_from_headers(x_forwarded_for: Optional[str], x_real_ip: Optional[str], peer: Optional[str]) -> Optional[str]:
    """Resolve the real client IP behind a proxy (Railway sets X-Forwarded-For)."""
    if x_forwarded_for:
        # left-most entry is the original client
        return x_forwarded_for.split(",")[0].strip()
    if x_real_ip:
        return x_real_ip.strip()
    return peer


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local)
    except ValueError:
        return False


async def lookup_ip(ip: str) -> Optional[dict]:
    """Return {country, region, city} for a public IP, or None on any failure."""
    if not ip or not _is_public(ip):
        return None
    url = f"https://ipwho.is/{ip}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params={"fields": "success,country,region,city,latitude,longitude"})
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("success"):
        return None
    lat, lng = data.get("latitude"), data.get("longitude")
    return {
        "country": data.get("country") or None,
        "region": data.get("region") or None,
        "city": data.get("city") or None,
        "lat": lat if isinstance(lat, (int, float)) else None,
        "lng": lng if isinstance(lng, (int, float)) else None,
    }
