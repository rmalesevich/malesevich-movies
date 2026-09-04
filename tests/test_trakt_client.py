"""Trakt HTTP client: pagination, error mapping, date bounds."""
from datetime import UTC, date

import httpx
import pytest

from app.services import trakt as trakt_module
from app.services.trakt import (
    TraktClient,
    TraktError,
    TraktPrivateProfile,
    TraktRateLimited,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, page_count=1, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.headers = {"X-Pagination-Page-Count": str(page_count)}
        self.headers.update(headers or {})

    def json(self):
        return self._payload


@pytest.fixture
def slept(monkeypatch):
    """Record every sleep the client takes instead of actually waiting."""
    waits = []
    monkeypatch.setattr(
        trakt_module.time_module, "sleep", lambda seconds: waits.append(seconds)
    )
    return waits


def client(**kwargs):
    """A client that does not throttle unless a test asks it to."""
    kwargs.setdefault("client_id", "abc")
    kwargs.setdefault("min_interval", 0)
    return TraktClient(**kwargs)


@pytest.fixture
def capture(monkeypatch):
    """Replace httpx.get and record the calls it receives."""
    calls = []

    def install(responder):
        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({"url": url, "params": params, "headers": headers})
            return responder(params.get("page", 1))

        monkeypatch.setattr(httpx, "get", fake_get)
        return calls

    return install


def test_pagination_follows_the_page_count_header(capture):
    pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}
    calls = capture(lambda page: FakeResponse(pages[page], page_count=2))

    items = client().movie_ratings("ann")

    assert [i["id"] for i in items] == [1, 2, 3]
    assert [c["params"]["page"] for c in calls] == [1, 2]


def test_pagination_stops_at_the_cap_and_warns(capture, monkeypatch, caplog):
    monkeypatch.setattr(trakt_module, "MAX_PAGES", 3)
    # Claims far more pages than the cap allows.
    capture(lambda page: FakeResponse([{"id": page}], page_count=99))

    with caplog.at_level("WARNING"):
        items = client().movie_ratings("ann")

    assert len(items) == 3  # one per page, stopped at the cap
    assert "cap" in caplog.text
    assert "99 pages available" in caplog.text


def test_an_empty_page_ends_pagination(capture):
    calls = capture(lambda page: FakeResponse([] if page > 1 else [{"id": 1}],
                                              page_count=5))

    items = client().movie_ratings("ann")

    assert len(items) == 1
    assert len(calls) == 2


def test_a_private_profile_raises_a_specific_error(capture):
    capture(lambda page: FakeResponse([], status_code=401))

    with pytest.raises(TraktPrivateProfile, match="private"):
        client().movie_ratings("ann")


def test_an_unknown_user_is_reported(capture):
    capture(lambda page: FakeResponse([], status_code=404))

    with pytest.raises(TraktError, match="not found"):
        client().movie_history("nobody")


def test_history_sends_the_round_window_as_iso_bounds(capture):
    calls = capture(lambda page: FakeResponse([]))

    client().movie_history(
        "ann", date(2024, 1, 1), date(2024, 2, 1)
    )

    params = calls[0]["params"]
    assert params["start_at"] == "2024-01-01T00:00:00.000Z"
    assert params["end_at"] == "2024-02-01T23:59:59.000Z"
    assert calls[0]["headers"]["trakt-api-key"] == "abc"


def test_ratings_are_requested_without_date_bounds(capture):
    """Ratings are deliberately unwindowed, so no start/end may be sent."""
    calls = capture(lambda page: FakeResponse([]))

    client().movie_ratings("ann")

    assert "start_at" not in calls[0]["params"]
    assert "end_at" not in calls[0]["params"]


def test_a_missing_client_id_is_rejected_before_any_request():
    with pytest.raises(TraktError, match="TRAKT_CLIENT_ID"):
        TraktClient(client_id="", min_interval=0).movie_ratings("ann")


# --- rate limiting --------------------------------------------------------
def test_a_429_is_retried_after_the_requested_delay(capture, slept):
    attempts = {"n": 0}

    def responder(page):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse([], status_code=429, headers={"Retry-After": "7"})
        return FakeResponse([{"id": 1}])

    capture(responder)

    items = client().movie_ratings("ann")

    assert [i["id"] for i in items] == [1]
    assert attempts["n"] == 2
    assert slept == [7.0]          # exactly what Retry-After asked for


def test_retry_after_is_capped(capture, slept, monkeypatch):
    """A manual sync blocks a web request, so an absurd delay is clamped."""
    monkeypatch.setattr(trakt_module.settings, "trakt_max_backoff", 30.0)
    attempts = {"n": 0}

    def responder(page):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse([], status_code=429,
                                headers={"Retry-After": "99999"})
        return FakeResponse([{"id": 1}])

    capture(responder)
    client().movie_ratings("ann")

    assert slept == [30.0]


def test_a_missing_retry_after_falls_back_to_one_second(capture, slept):
    attempts = {"n": 0}

    def responder(page):
        attempts["n"] += 1
        return (FakeResponse([], status_code=429) if attempts["n"] == 1
                else FakeResponse([{"id": 1}]))

    capture(responder)
    client().movie_ratings("ann")

    assert slept == [1.0]


def test_persistent_429_raises_rather_than_looping(capture, slept):
    capture(lambda page: FakeResponse([], status_code=429,
                                      headers={"Retry-After": "2"}))

    with pytest.raises(TraktRateLimited, match="still limited after"):
        client(max_retries=2).movie_ratings("ann")

    assert slept == [2.0, 2.0]     # two retries, then it gives up


def test_requests_are_spaced_by_the_minimum_interval(capture, slept):
    """The first call goes straight out; later ones wait."""
    pages = {1: [{"id": 1}], 2: [{"id": 2}]}
    capture(lambda page: FakeResponse(pages[page], page_count=2))

    client(min_interval=0.4).movie_ratings("ann")

    # One sleep, before the second request, of at most the interval.
    assert len(slept) == 1
    assert 0 < slept[0] <= 0.4


def test_a_nearly_spent_budget_triggers_a_pause(capture, slept):
    from datetime import datetime, timedelta

    until = (datetime.now(UTC) + timedelta(seconds=20)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    budget = (
        '{"name":"UNAUTHED_API_GET_LIMIT","period":300,"limit":1000,'
        f'"remaining":3,"until":"{until}"}}'
    )
    capture(lambda page: FakeResponse([{"id": 1}], headers={"X-Ratelimit": budget}))

    client().movie_ratings("ann")

    assert len(slept) == 1
    assert 0 < slept[0] <= 20


def test_a_healthy_budget_does_not_pause(capture, slept):
    budget = ('{"name":"UNAUTHED_API_GET_LIMIT","period":300,"limit":1000,'
              '"remaining":900,"until":"2030-01-01T00:00:00.000Z"}')
    capture(lambda page: FakeResponse([{"id": 1}], headers={"X-Ratelimit": budget}))

    client().movie_ratings("ann")

    assert slept == []


def test_a_malformed_budget_header_is_ignored(capture, slept):
    capture(lambda page: FakeResponse([{"id": 1}],
                                      headers={"X-Ratelimit": "not json"}))

    assert len(client().movie_ratings("ann")) == 1
    assert slept == []


# --- caching --------------------------------------------------------------
def test_ratings_are_fetched_once_per_user(capture):
    """The fix for the 429 storm: 76 rounds must not mean 76 ratings fetches."""
    calls = capture(lambda page: FakeResponse([{"id": 1}]))
    c = client()

    first = c.movie_ratings("ann")
    for _ in range(75):
        c.movie_ratings("ann")

    assert len(calls) == 1
    assert c.movie_ratings("ann") is first
    assert c.request_count == 1


def test_each_user_is_cached_separately(capture):
    calls = capture(lambda page: FakeResponse([{"id": 1}]))
    c = client()

    c.movie_ratings("ann")
    c.movie_ratings("bob")
    c.movie_ratings("ann")

    assert len(calls) == 2
    assert sorted(c._ratings_cache) == ["ann", "bob"]


def test_clearing_the_cache_forces_a_refetch(capture):
    calls = capture(lambda page: FakeResponse([{"id": 1}]))
    c = client()

    c.movie_ratings("ann")
    c.clear_cache()
    c.movie_ratings("ann")

    assert len(calls) == 2


def test_history_is_not_cached(capture):
    """History is date-bounded per round, so it must be refetched each time."""
    calls = capture(lambda page: FakeResponse([]))
    c = client()

    c.movie_history("ann", date(2024, 1, 1), date(2024, 2, 1))
    c.movie_history("ann", date(2024, 3, 1), date(2024, 4, 1))

    assert len(calls) == 2
