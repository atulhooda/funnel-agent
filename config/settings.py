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


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so config is read once per process."""
    return Settings()
