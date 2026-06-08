"""Runtime configuration: search defaults, request delays, output path.

Backed by ``pydantic-settings`` so values can be overridden via environment
variables (prefix ``LJP_``) or a local ``.env`` file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Application configuration."""

    model_config = SettingsConfigDict(env_prefix="LJP_", env_file=".env", extra="ignore")

    # --- search defaults ---
    default_keywords: str = "python"
    default_location: str | None = None

    # --- politeness / rate limiting ---
    request_delay_seconds: float = 2.0
    request_jitter_seconds: float = 1.0
    max_pages: int = 40
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    # --- output ---
    db_path: str = "output/jobs.db"


config = Config()
