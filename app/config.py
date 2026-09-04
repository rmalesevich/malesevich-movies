"""Application settings, loaded from environment / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---
    app_name: str = "Malesevich Movies"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'malesevich.db'}"
    secret_key: str = "change-me-in-production"
    debug: bool = False

    # --- Auth ---
    # Single shared password. Leave blank to disable the login wall entirely.
    app_password: str = ""
    session_max_age: int = 60 * 60 * 24 * 30  # 30 days

    # --- TheMovieDB ---
    tmdb_api_key: str = ""
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    tmdb_image_base: str = "https://image.tmdb.org/t/p"
    tmdb_language: str = "en-US"

    # --- Trakt ---
    # Only the client id is needed to read *public* profiles.
    trakt_client_id: str = ""
    trakt_base_url: str = "https://api.trakt.tv"
    # Trakt allows 1000 GETs per 5 minutes (~3.3/sec). Pace well under that:
    # 0.4s between calls is ~150/min, comfortably inside the budget.
    trakt_min_interval: float = 0.4
    # How many times to retry a 429 before giving up, and the longest we will
    # honour a Retry-After. The cap matters because a manual sync from the web
    # UI blocks the request while it waits.
    trakt_max_retries: int = 3
    trakt_max_backoff: float = 60.0

    # --- Sync scheduler ---
    sync_enabled: bool = True
    sync_hour: int = 4  # local hour of day for the daily Trakt sync
    sync_minute: int = 30
    timezone: str = "America/New_York"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)

    @property
    def tmdb_enabled(self) -> bool:
        return bool(self.tmdb_api_key)

    @property
    def trakt_enabled(self) -> bool:
        return bool(self.trakt_client_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
