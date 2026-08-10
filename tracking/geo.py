"""Location resolution — two sources, very different precision.

  1. IP lookup (automatic, no consent needed, ALWAYS approximate).
     An IP resolves to the ISP's registered gateway, not the person. On fixed
     broadband that is usually the right city and often the right postal code.
     On mobile carriers it is frequently a different city — a Jio subscriber in
     Pune commonly resolves to Mumbai — so this can never answer "where in Pune".
     Stored with location_source='ip' and accuracy_m=NULL so the UI can say so.

  2. Browser Geolocation (opt-in, requires the visitor to accept the prompt).
     GPS/Wi-Fi trilateration, accurate to tens of metres, and reverse-geocoded to
     a neighbourhood and street. This is the only source that gives a real answer
     to "where exactly". Stored with location_source='gps' and the browser's own
     accuracy radius in accuracy_m.

Both run in the background from ingestion so they never slow /track. The raw IP
is used transiently and never stored. Any failure is swallowed — location is a
nice-to-have, not required.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import Optional

import httpx

# Nominatim asks every caller to identify itself and to stay under ~1 req/sec.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "behavioral-funnel-agent/1.0 (visitor analytics; self-hosted)"
_nominatim_lock = asyncio.Lock()


def client_ip_from_headers(
    x_forwarded_for: Optional[str],
    x_real_ip: Optional[str],
    peer: Optional[str],
    cf_connecting_ip: Optional[str] = None,
) -> Optional[str]:
    """Resolve the real client IP behind the proxy chain.

    Order matters. With Cloudflare in front of Railway there are two proxies, and
    X-Forwarded-For becomes a list that a client can also prepend to. Cloudflare's
    own CF-Connecting-IP is a single value it sets itself, so it is both more
    trustworthy and more accurate than the left-most XFF entry — without it, geo
    for every visitor collapses to whatever the first hop reports.
    """
    if cf_connecting_ip:
        return cf_connecting_ip.strip()
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


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def lookup_ip(ip: str) -> Optional[dict]:
    """Approximate location for a public IP, or None on any failure.

    Returns country/region/city plus postal and ISP — the ISP matters because it
    tells you how much to trust the city (a mobile carrier means "somewhere in
    this state", a local broadband provider means "probably this neighbourhood").
    """
    if not ip or not _is_public(ip):
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://ipwho.is/{ip}")
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("success"):
        return None

    lat, lng = data.get("latitude"), data.get("longitude")
    connection = data.get("connection") or {}
    return {
        "country": _clean(data.get("country")),
        "region": _clean(data.get("region")),
        "city": _clean(data.get("city")),
        "postal": _clean(data.get("postal")),
        "isp": _clean(connection.get("isp") or connection.get("org")),
        "lat": lat if isinstance(lat, (int, float)) else None,
        "lng": lng if isinstance(lng, (int, float)) else None,
    }


async def reverse_geocode(lat: float, lng: float) -> Optional[dict]:
    """Turn precise coordinates into a street + neighbourhood (OpenStreetMap).

    Only worth calling on a GPS fix. Running it on IP coordinates would invent
    detail that isn't there — the coordinates are a city centroid, so it would
    confidently name whatever neighbourhood happens to sit at the middle of town.
    """
    params = {
        "lat": lat, "lon": lng, "format": "jsonv2",
        "zoom": 18,               # building/street level
        "addressdetails": 1,
    }
    try:
        # Serialize calls: Nominatim's usage policy is ~1 request per second.
        async with _nominatim_lock:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": _USER_AGENT}) as client:
                resp = await client.get(_NOMINATIM_URL, params=params)
            await asyncio.sleep(1.0)
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    address = data.get("address") or {}
    return {
        "street": _clean(address.get("road") or address.get("pedestrian") or address.get("footway")),
        "district": _clean(
            address.get("neighbourhood") or address.get("suburb")
            or address.get("city_district") or address.get("quarter")
            or address.get("village") or address.get("hamlet")
        ),
        "city": _clean(
            address.get("city") or address.get("town")
            or address.get("municipality") or address.get("county")
        ),
        "region": _clean(address.get("state")),
        "country": _clean(address.get("country")),
        "postal": _clean(address.get("postcode")),
        "display_name": _clean(data.get("display_name")),
    }
