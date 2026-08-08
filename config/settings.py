"""Environment settings (DATABASE_URL, GEMINI_API_KEY, GEMINI_MODEL, SITE_ID...).

pydantic-settings loader. Wiring/env only — no business rules live here.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/funnel_agent"

    # Gemini (used by Layers 2 & 3; unused in Layer 1)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # App / tenancy
    site_id: str = "default"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Going live: browser ingestion + safety
    track_write_key: str = ""            # if set, /track & /identify require header X-Write-Key
    cors_allow_origins: str = "*"        # comma-separated site origins allowed to POST from the browser
    execution_mode: str = "shadow"       # "shadow" = always stub sends; "live" = use real senders

    # Admin auth — HTTP Basic gate on the dashboard + internal /score, /decide, /execute
    # routes. Public ingestion (/track, /identify) and /health are NOT gated.
    # Fails CLOSED: if admin_password is empty, protected routes return 503, never open.
    admin_user: str = "admin"
    admin_password: str = ""             # REQUIRED in any public deploy; unset = dashboard locked

    # Meta WhatsApp Cloud API — real WhatsApp sender (used only when EXECUTION_MODE=live)
    meta_wa_access_token: str = ""            # System User permanent token (whatsapp_business_messaging)
    meta_wa_phone_number_id: str = ""         # the WABA phone number ID (not the phone number)
    meta_wa_api_version: str = "v21.0"
    meta_wa_message_type: str = "text"        # "text" (only inside 24h window) or "template" (business-initiated)
    meta_wa_template_name: str = ""           # required when message_type=template
    meta_wa_template_lang: str = "en_US"
    meta_wa_template_body_param: bool = True   # False for zero-variable templates (e.g. hello_world)

    # Inbound WhatsApp webhook (Meta calls US). Both are required to receive:
    #   verify_token — the string you type into Meta's "Verify token" box; echoed
    #                  back during the one-time GET handshake.
    #   app_secret   — Meta app secret, used to check the X-Hub-Signature-256 HMAC
    #                  on every POST. Without it the endpoint is unauthenticated
    #                  and anyone could inject fake messages, so it fails closed.
    meta_wa_verify_token: str = ""
    meta_wa_app_secret: str = ""
    # Meta allows ONE callback URL per app. If another service (e.g. an OTP flow)
    # needs the same events, set this to its URL and every verified payload is
    # forwarded there verbatim, best-effort, after we store it.
    meta_wa_forward_url: str = ""

    # Apply schema.sql on startup (idempotent) — makes deploy one-click on any host.
    run_schema_on_startup: bool = True

    # Autonomous scheduler — runs the pipeline (score -> decide -> execute) on a
    # loop so live traffic becomes scored visitors + decisions with no manual
    # trigger. Only new/changed leads are processed each cycle.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so config is read once per process."""
    return Settings()
