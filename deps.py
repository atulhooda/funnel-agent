"""Shared FastAPI dependencies.

Kept tiny and layer-agnostic so every router (tracking now, dashboard later)
resolves tenancy the same way.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from config.settings import get_settings


def get_site_id(x_site_id: Optional[str] = Header(default=None, alias="X-Site-Id")) -> str:
    """Resolve the tenant for a request.

    Prototype default is the configured SITE_ID ('default'), but an X-Site-Id
    header already overrides it — so nothing hardcodes single-tenancy.
    """
    return x_site_id or get_settings().site_id


def require_write_key(x_write_key: Optional[str] = Header(default=None, alias="X-Write-Key")) -> bool:
    """Gate the public ingestion endpoints (/track, /identify).

    If TRACK_WRITE_KEY is configured, the browser snippet must send a matching
    X-Write-Key. Unset (dev) = no enforcement, so local scripts keep working.
    """
    key = get_settings().track_write_key
    if key and x_write_key != key:
        raise HTTPException(status_code=401, detail="invalid or missing write key")
    return True
