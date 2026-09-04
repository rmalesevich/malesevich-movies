"""Trakt HTTP client: pagination, error mapping, date bounds."""
import httpx
import pytest

from app.services import trakt as trakt_module
from app.services.trakt import TraktClient, TraktError, TraktPrivateProfile


class FakeResponse:
    def __init__(self, payload, status_code=200, page_count=1):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.headers = {"X-Pagination-Page-Count": str(page_count)}

    def json(self):
        return self._payload


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

    items = TraktClient(client_id="abc").movie_ratings("ann")

    assert [i["id"] for i in items] == [1, 2, 3]
    assert [c["params"]["page"] for c in calls] == [1, 2]


def test_pagination_stops_at_the_cap_and_warns(capture, monkeypatch, caplog):
    monkeypatch.setattr(trakt_module, "MAX_PAGES", 3)
    # Claims far more pages than the cap allows.
    capture(lambda page: FakeResponse([{"id": page}], page_count=99))

    with caplog.at_level("WARNING"):
        items = TraktClient(client_id="abc").movie_ratings("ann")

    assert len(items) == 3  # one per page, stopped at the cap
    assert "cap" in caplog.text
    assert "99 pages available" in caplog.text


def test_an_empty_page_ends_pagination(capture):
    calls = capture(lambda page: FakeResponse([] if page > 1 else [{"id": 1}],
                                              page_count=5))

    items = TraktClient(client_id="abc").movie_ratings("ann")

    assert len(items) == 1
    assert len(calls) == 2


def test_a_private_profile_raises_a_specific_error(capture):
    capture(lambda page: FakeResponse([], status_code=401))

    with pytest.raises(TraktPrivateProfile, match="private"):
        TraktClient(client_id="abc").movie_ratings("ann")


def test_an_unknown_user_is_reported(capture):
    capture(lambda page: FakeResponse([], status_code=404))

    with pytest.raises(TraktError, match="not found"):
        TraktClient(client_id="abc").movie_history("nobody")


def test_history_sends_the_round_window_as_iso_bounds(capture):
    from datetime import date

    calls = capture(lambda page: FakeResponse([]))

    TraktClient(client_id="abc").movie_history(
        "ann", date(2024, 1, 1), date(2024, 2, 1)
    )

    params = calls[0]["params"]
    assert params["start_at"] == "2024-01-01T00:00:00.000Z"
    assert params["end_at"] == "2024-02-01T23:59:59.000Z"
    assert calls[0]["headers"]["trakt-api-key"] == "abc"


def test_ratings_are_requested_without_date_bounds(capture):
    """Ratings are deliberately unwindowed, so no start/end may be sent."""
    calls = capture(lambda page: FakeResponse([]))

    TraktClient(client_id="abc").movie_ratings("ann")

    assert "start_at" not in calls[0]["params"]
    assert "end_at" not in calls[0]["params"]


def test_a_missing_client_id_is_rejected_before_any_request():
    with pytest.raises(TraktError, match="TRAKT_CLIENT_ID"):
        TraktClient(client_id="").movie_ratings("ann")
