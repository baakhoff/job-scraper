"""Runtime configuration: search defaults, request delays, output path.

Backed by ``pydantic-settings`` so values can be overridden via environment
variables (prefix ``LJP_``) or a local ``.env`` file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# A small pool of realistic desktop browser User-Agents. The scraper rotates
# through these per request so traffic doesn't look like a single client
# hammering the endpoint. Keep them current-ish; very old UAs get blocked.
DEFAULT_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) "
    "Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_2) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
]


class Config(BaseSettings):
    """Application configuration."""

    model_config = SettingsConfigDict(env_prefix="LJP_", env_file=".env", extra="ignore")

    # --- search defaults ---
    default_keywords: str = "python"
    default_location: str | None = None
    max_results: int = 75

    # --- politeness / rate limiting ---
    # Random delay between requests is drawn uniformly from [min, max].
    request_delay_min: float = 2.0
    request_delay_max: float = 5.0
    max_pages: int = 40
    max_retries: int = 4
    user_agents: list[str] = DEFAULT_USER_AGENTS

    # --- output ---
    db_path: str = "output/jobs.db"

    # --- enrichment & people discovery ---
    # Whether the Companies/Explore flows may fetch public company pages to fill
    # in industry/size/website/description. Best-effort and selector-fragile.
    company_enrich_enabled: bool = True
    # CEO/Founder discovery. OFF by default: LinkedIn's public people search is
    # generally login-gated, so the default provider returns nothing. Set the
    # provider to "linkedin" to attempt the best-effort scrape anyway.
    people_search_enabled: bool = True
    people_provider: str = "linkedin"  # one of: "null", "linkedin"
    # Keywords used to find leadership when searching people at a company.
    people_search_keywords: list[str] = ["CEO", "Founder", "Co-Founder", "Owner"]

    # --- database ---
    # SQLAlchemy URL for the persistence layer. Defaults to a local async SQLite
    # file for bare-CLI use; Docker overrides this with a Postgres asyncpg URL via
    # the ``DATABASE_URL`` environment variable (read directly in src/storage.py).
    database_url: str = "sqlite+aiosqlite:///./jobs.db"


config = Config()
