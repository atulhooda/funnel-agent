"""Shared FastAPI dependencies.

Kept tiny and layer-agnostic so every router (tracking now, dashboard later)
resolves tenancy the same way.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config.settings import get_settings

_basic = HTTPBasic(auto_error=False)


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


def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)) -> bool:
    """Gate the dashboard + internal control routes (/score, /decide, /execute).

    HTTP Basic auth against ADMIN_USER / ADMIN_PASSWORD. Fails CLOSED: if no
    password is configured, every protected route returns 503 rather than being
    served open — so a public deploy that forgot to set credentials is locked,
    not exposed. Uses constant-time comparison to avoid timing leaks.
    """
    settings = get_settings()
    expected_user, expected_password = settings.admin_user, settings.admin_password

    if not expected_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin auth not configured — set ADMIN_USER and ADMIN_PASSWORD",
        )

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
    if credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and pass_ok):
        raise unauthorized
    return True
