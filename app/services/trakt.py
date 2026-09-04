"""Trakt client.

Only public profiles are read, so a client id is enough - no OAuth dance and
no per-participant tokens. If a participant's Trakt profile is private the API
returns 401/403 and we surface that as a clear error in the sync log.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

PAGE_LIMIT = 100
MAX_PAGES = 200  # safety valve against a runaway pagination loop (20k records)


class TraktError(RuntimeError):
    pass


class TraktPrivateProfile(TraktError):
    """The profile exists but is not publicly readable."""


def _to_iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_trakt_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TraktClient:
    def __init__(self, client_id: str | None = None, base_url: str | None = None):
        self.client_id = (
            client_id if client_id is not None else settings.trakt_client_id
        )
        self.base_url = (base_url or settings.trakt_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        if not self.client_id:
            raise TraktError("TRAKT_CLIENT_ID is not configured")
        page = 1
        while True:
            query = dict(params or {})
            query.update({"limit": PAGE_LIMIT, "page": page})
            url = f"{self.base_url}{path}"
            try:
                resp = httpx.get(
                    url, params=query, headers=self._headers(), timeout=30.0
                )
            except httpx.HTTPError as exc:
                raise TraktError(f"Trakt request failed for {path}: {exc}") from exc

            if resp.status_code in (401, 403):
                raise TraktPrivateProfile(
                    f"Trakt returned {resp.status_code} for {path} - the profile is "
                    "likely private. Make it public or add an OAuth token."
                )
            if resp.status_code == 404:
                raise TraktError(f"Trakt user or resource not found: {path}")
            if resp.status_code >= 400:
                raise TraktError(
                    f"Trakt {resp.status_code} for {path}: {resp.text[:200]}"
                )

            items = resp.json()
            if not items:
                return
            yield from items

            page_count = int(resp.headers.get("X-Pagination-Page-Count", page) or page)
            if page >= page_count:
                return
            if page >= MAX_PAGES:
                # Ratings are read in full (they are not date-filtered), so a
                # silent truncation here would look like a missing score.
                log.warning(
                    "Stopped paginating %s at the %d-page cap (%d pages available); "
                    "some records were not read. Raise MAX_PAGES if this is real.",
                    path,
                    MAX_PAGES,
                    page_count,
                )
                return
            page += 1

    # -- endpoints ---------------------------------------------------------
    def movie_history(
        self, username: str, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        """Movie plays for a user, optionally bounded by a date range."""
        params: dict[str, Any] = {}
        if start:
            params["start_at"] = _to_iso_z(datetime.combine(start, time.min))
        if end:
            params["end_at"] = _to_iso_z(datetime.combine(end, time.max))
        return list(self._paginate(f"/users/{username}/history/movies", params))

    def movie_ratings(self, username: str) -> list[dict[str, Any]]:
        """All movie ratings for a user. Trakt has no date filter here, so the
        round window is applied by the caller against ``rated_at``."""
        return list(self._paginate(f"/users/{username}/ratings/movies"))
