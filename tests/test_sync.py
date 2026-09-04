"""Trakt reconciliation: only films in the round, only inside its window."""
from datetime import date, datetime

from app.models import Rating, RoundStatus, Watch
from app.services.sync import sync_round
from app.services.trakt import TraktError
from tests import factories as f


def history_item(tmdb_id: int, watched_at: str, title="A Film"):
    return {
        "watched_at": watched_at,
        "action": "watch",
        "type": "movie",
        "movie": {"title": title, "ids": {"trakt": tmdb_id + 5, "tmdb": tmdb_id,
                                          "slug": f"film-{tmdb_id}"}},
    }


def rating_item(tmdb_id: int, rating: int, rated_at: str, title="A Film"):
    return {
        "rated_at": rated_at,
        "rating": rating,
        "type": "movie",
        "movie": {"title": title, "ids": {"trakt": tmdb_id + 5, "tmdb": tmdb_id}},
    }


class FakeTrakt:
    """Stands in for TraktClient; records the date window it was asked for."""

    def __init__(self, history=None, ratings=None, fail_history=False):
        self.history = history or {}
        self.ratings = ratings or {}
        self.fail_history = fail_history
        self.calls = []

    def movie_history(self, username, start=None, end=None):
        self.calls.append((username, start, end))
        if self.fail_history:
            raise TraktError("profile is private")
        return self.history.get(username, [])

    def movie_ratings(self, username):
        return self.ratings.get(username, [])


def build_round(db):
    ann = f.make_participant(db, "Ann", trakt_username="ann")
    bob = f.make_participant(db, "Bob", trakt_username="bob")
    cy = f.make_participant(db, "Cy")  # no Trakt account
    heat = f.make_movie(db, "Heat", tmdb_id=949)
    fargo = f.make_movie(db, "Fargo", tmdb_id=275)
    round_ = f.make_round(db, 1, [ann, bob, cy], started_on=date(2024, 1, 1),
                          discussed_on=date(2024, 2, 1),
                          status=RoundStatus.CLOSED)
    f.make_pick(db, round_, ann, heat)
    f.make_pick(db, round_, bob, fargo)
    db.commit()
    return round_, ann, bob, cy, heat, fargo


def test_sync_records_watches_and_ratings(db):
    round_, ann, bob, cy, heat, fargo = build_round(db)
    client = FakeTrakt(
        history={
            "ann": [history_item(949, "2024-01-10T20:00:00.000Z"),
                    history_item(275, "2024-01-12T20:00:00.000Z")],
            "bob": [history_item(949, "2024-01-20T20:00:00.000Z")],
        },
        ratings={"ann": [rating_item(949, 9, "2024-01-11T09:00:00.000Z")]},
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.ok
    assert result.watches_added == 3
    assert result.ratings_added == 1

    watches = db.query(Watch).all()
    assert {(w.participant_id, w.movie_id) for w in watches} == {
        (ann.id, heat.id), (ann.id, fargo.id), (bob.id, heat.id)
    }
    rating = db.query(Rating).one()
    assert rating.rating == 9 and rating.participant_id == ann.id

    # The round window is passed through to Trakt, and Cy is skipped entirely.
    assert ("ann", date(2024, 1, 1), date(2024, 2, 1)) in client.calls
    assert not any(call[0] is None for call in client.calls)
    assert len(client.calls) == 2


def test_films_outside_the_round_are_ignored(db):
    round_, ann, *_ = build_round(db)
    client = FakeTrakt(
        history={"ann": [history_item(999999, "2024-01-10T20:00:00.000Z", "Unrelated")]}
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.watches_added == 0
    assert db.query(Watch).count() == 0


def test_ratings_from_before_the_round_are_kept(db):
    """A rating is a standing opinion, not an event inside the round window.

    The Dark City case: seen and rated years earlier, never re-rated, so a
    window filter would silently lose the score.
    """
    round_, ann, *_ = build_round(db)
    client = FakeTrakt(
        ratings={
            "ann": [
                rating_item(949, 10, "2019-05-01T09:00:00.000Z"),  # long before
                rating_item(275, 6, "2024-01-15T09:00:00.000Z"),   # during
            ]
        }
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.ratings_added == 2
    scores = {r.movie_id: r.rating for r in db.query(Rating).all()}
    assert sorted(scores.values()) == [6, 10]
    # The original rating date is preserved, so the UI can tell them apart.
    old_rating = db.query(Rating).filter_by(rating=10).one()
    assert old_rating.rated_at == datetime(2019, 5, 1, 9, 0)


def test_a_rating_without_a_watch_in_the_round_is_still_recorded(db):
    """Ratings and watches are scoped independently."""
    round_, ann, bob, cy, heat, fargo = build_round(db)
    client = FakeTrakt(
        history={},  # nobody watched anything during the round
        ratings={"ann": [rating_item(949, 8, "2018-02-02T09:00:00.000Z")]},
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.watches_added == 0
    assert db.query(Watch).count() == 0
    assert result.ratings_added == 1
    rating = db.query(Rating).one()
    assert rating.rating == 8
    assert rating.movie_id == heat.id
    assert rating.participant_id == ann.id


def test_ratings_for_films_outside_the_round_are_still_ignored(db):
    """Dropping the date filter must not widen *which films* count."""
    round_, ann, *_ = build_round(db)
    client = FakeTrakt(
        ratings={"ann": [rating_item(999999, 10, "2020-01-01T09:00:00.000Z")]}
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.ratings_added == 0
    assert db.query(Rating).count() == 0


def test_sync_is_idempotent_and_keeps_the_earliest_play(db):
    round_, ann, bob, cy, heat, fargo = build_round(db)
    first = FakeTrakt(history={"ann": [history_item(949, "2024-01-20T20:00:00.000Z")]})
    sync_round(db, round_, first)
    db.commit()
    assert db.query(Watch).count() == 1

    # A re-run that also reports an earlier play for the same film.
    second = FakeTrakt(
        history={"ann": [history_item(949, "2024-01-20T20:00:00.000Z"),
                         history_item(949, "2024-01-05T20:00:00.000Z")]}
    )
    result = sync_round(db, round_, second)
    db.commit()

    assert result.watches_added == 0
    assert db.query(Watch).count() == 1
    assert db.query(Watch).one().watched_at == datetime(2024, 1, 5, 20, 0)


def test_trakt_ids_are_backfilled_onto_the_movie(db):
    round_, ann, bob, cy, heat, fargo = build_round(db)
    assert heat.trakt_id is None

    sync_round(db, round_, FakeTrakt(
        history={"ann": [history_item(949, "2024-01-10T20:00:00.000Z")]}
    ))
    db.commit()
    db.refresh(heat)

    assert heat.trakt_id == 954
    assert heat.trakt_slug == "film-949"


def test_a_private_profile_is_reported_not_raised(db):
    round_, *_ = build_round(db)
    result = sync_round(db, round_, FakeTrakt(fail_history=True))

    assert not result.ok
    assert len(result.errors) == 2  # one per participant with a Trakt account
    assert "private" in result.errors[0]


def test_repeat_plays_are_counted_not_duplicated(db):
    """Trakt returns one row per play; a film watched twice is one Watch."""
    round_, ann, bob, cy, heat, fargo = build_round(db)
    client = FakeTrakt(
        history={
            "ann": [
                history_item(949, "2024-01-20T20:00:00.000Z"),
                history_item(949, "2024-01-05T20:00:00.000Z"),
                history_item(949, "2024-01-28T20:00:00.000Z"),
            ]
        }
    )

    result = sync_round(db, round_, client)
    db.commit()

    assert result.watches_added == 1
    watch = db.query(Watch).one()
    assert watch.plays == 3
    assert watch.watched_at == datetime(2024, 1, 5, 20, 0)  # earliest in window

    # Re-syncing the same history changes nothing.
    result = sync_round(db, round_, client)
    db.commit()
    assert result.watches_added == 0
    assert result.watches_updated == 0
    assert db.query(Watch).count() == 1
    assert db.query(Watch).one().plays == 3
