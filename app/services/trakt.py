"""Trakt client.

Only public profiles are read, so a client id is enough - no OAuth dance and
no per-participant tokens. If a participant's Trakt profile is private the API
returns 401/403 and we surface that as a clear error in the sync log.
"""
from __future__ import annotations

import json
import logging
import time as time_module
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


class TraktRateLimited(TraktError):
    """Still rate limited after exhausting retries."""


# Slow down pre-emptively once the remaining budget drops this low, rather than
# sprinting into a 429.
LOW_REMAINING = 50


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
    """Reads public Trakt profiles, politely.

    Two things keep this inside Trakt's budget of 1000 GETs per 5 minutes:

    * every request is spaced by ``min_interval`` and backs off on a 429,
      honouring the ``Retry-After`` header;
    * a user's ratings are fetched at most once per client, since the list is
      account-wide rather than per-round. Syncing 76 rounds for 4 people used
      to mean 304 full ratings fetches; it now means 4.

    A client is therefore worth reusing across rounds, and is *not* safe to
    share between threads.
    """

    def __init__(
        self,
        client_id: str | None = None,
        base_url: str | None = None,
        min_interval: float | None = None,
        max_retries: int | None = None,
    ):
        self.client_id = (
            client_id if client_id is not None else settings.trakt_client_id
        )
        self.base_url = (base_url or settings.trakt_base_url).rstrip("/")
        self.min_interval = (
            settings.trakt_min_interval if min_interval is None else min_interval
        )
        self.max_retries = (
            settings.trakt_max_retries if max_retries is None else max_retries
        )
        self._last_request_at: float | None = None
        self._ratings_cache: dict[str, list[dict[str, Any]]] = {}
        self.request_count = 0

    # -- politeness --------------------------------------------------------
    def _throttle(self) -> None:
        """Space requests out so we never sprint at the limit."""
        if self._last_request_at is None or self.min_interval <= 0:
            return
        elapsed = time_module.monotonic() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time_module.sleep(remaining)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        raw = resp.headers.get("Retry-After", "")
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            wait = 1.0
        # Never block a web request for longer than the configured ceiling.
        return max(0.0, min(wait, settings.trakt_max_backoff))

    def _check_budget(self, resp: httpx.Response) -> None:
        """Ease off when Trakt says the remaining budget is nearly spent.

        The X-Ratelimit header is JSON, e.g.
        {"name":"UNAUTHED_API_GET_LIMIT","period":300,"limit":1000,
         "remaining":12,"until":"2026-09-04T18:00:00Z"}

        Trakt does not send it on unauthenticated GETs today - this is a
        best-effort extra. The protections that actually carry the load are the
        fixed interval in _throttle() and the 429 backoff in _get().
        """
        raw = resp.headers.get("X-Ratelimit")
        if not raw:
            return
        try:
            budget = json.loads(raw)
            remaining = int(budget.get("remaining", LOW_REMAINING + 1))
        except (ValueError, TypeError):
            return
        if remaining > LOW_REMAINING:
            return

        until = parse_trakt_datetime(budget.get("until"))
        wait = 0.0
        if until is not None:
            now = datetime.now(UTC)
            wait = max(0.0, (until - now).total_seconds())
        wait = min(wait, settings.trakt_max_backoff)
        log.warning(
            "Trakt budget nearly spent (%s remaining); pausing %.0fs", remaining, wait
        )
        if wait > 0:
            time_module.sleep(wait)

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """One GET, throttled, with 429 retries."""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = httpx.get(
                    url, params=params, headers=self._headers(), timeout=30.0
                )
            except httpx.HTTPError as exc:
                raise TraktError(f"Trakt request failed for {url}: {exc}") from exc
            finally:
                self._last_request_at = time_module.monotonic()
            self.request_count += 1

            if resp.status_code != 429:
                self._check_budget(resp)
                return resp

            if attempt >= self.max_retries:
                raise TraktRateLimited(
                    f"Trakt rate limit hit for {url} and still limited after "
                    f"{self.max_retries} retries. Try again in a few minutes."
                )
            wait = self._retry_after(resp)
            log.warning(
                "Trakt rate limited (429); waiting %.1fs then retrying (%d/%d)",
                wait,
                attempt + 1,
                self.max_retries,
            )
            time_module.sleep(wait)

        raise TraktRateLimited(f"Trakt rate limit retries exhausted for {url}")

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
            resp = self._get(url, query)

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
        """Every movie rating for a user, fetched at most once per client.

        Ratings are account-wide, not per-round, so syncing many rounds would
        otherwise refetch the identical list for each one - which is exactly
        what used to exhaust the rate limit on a full historical import.
        """
        cached = self._ratings_cache.get(username)
        if cached is not None:
            log.debug("Reusing cached Trakt ratings for %s", username)
            return cached
        ratings = list(self._paginate(f"/users/{username}/ratings/movies"))
        self._ratings_cache[username] = ratings
        return ratings

    def clear_cache(self) -> None:
        """Drop cached ratings, e.g. between scheduled runs."""
        self._ratings_cache.clear()
