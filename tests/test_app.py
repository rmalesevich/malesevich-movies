"""End-to-end smoke coverage over the views, admin flows and statistics."""
from datetime import date

import pytest

from app.models import CreditKind, Participant, Pick, Round, RoundStatus
from app.services import rounds as round_service
from app.services import stats as stats_service
from tests import factories as f


@pytest.fixture
def world(db):
    """Two rounds: round 1 with three people, round 2 with a fourth added."""
    ann = f.make_participant(db, "Ann", joined_round=1, trakt_username="ann")
    bob = f.make_participant(db, "Bob", joined_round=1)
    cy = f.make_participant(db, "Cy", joined_round=1)
    dee = f.make_participant(db, "Dee", joined_round=2)

    heat = f.make_movie(db, "Heat", runtime=170, year=1995)
    fargo = f.make_movie(db, "Fargo", runtime=98, year=1996)
    ronin = f.make_movie(db, "Ronin", runtime=122, year=1998)
    drive = f.make_movie(db, "Drive", runtime=100, year=2011)

    f.credit(db, heat, "Michael Mann", CreditKind.CREW, job="Director")
    f.credit(db, ronin, "John Frankenheimer", CreditKind.CREW, job="Director")
    f.credit(db, fargo, "Joel Coen", CreditKind.CREW, job="Director")
    f.credit(db, drive, "Nicolas Winding Refn", CreditKind.CREW, job="Director")
    for movie in (heat, ronin):
        f.credit(db, movie, "Robert De Niro", CreditKind.CAST, order=0)
    f.credit(db, heat, "Al Pacino", CreditKind.CAST, order=1)

    r1 = f.make_round(db, 1, [ann, bob, cy], status=RoundStatus.CLOSED,
                      started_on=date(2024, 1, 1), discussed_on=date(2024, 2, 1))
    f.make_pick(db, r1, ann, heat)
    f.make_pick(db, r1, bob, fargo)
    f.make_pick(db, r1, cy, ronin)

    r2 = f.make_round(db, 2, [ann, bob, cy, dee], started_on=date(2024, 2, 2))
    f.make_pick(db, r2, ann, drive)

    f.make_watch(db, r2, bob, drive)
    f.make_rating(db, r2, bob, drive, score=9)

    db.commit()
    return {"r1": r1, "r2": r2, "ann": ann, "dee": dee, "drive": drive}


# --- public views ---------------------------------------------------------
def test_current_round_page(client, world):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Round 2" in body
    assert "Drive" in body
    # Bob logged a watch and a 9; the others have not.
    assert "✓" in body and "○" in body
    assert ">9<" in body


def test_rounds_index_and_detail(client, world):
    assert client.get("/rounds").status_code == 200
    detail = client.get("/rounds/1")
    assert detail.status_code == 200
    assert "Heat" in detail.text and "Fargo" in detail.text
    assert client.get("/rounds/999").status_code == 404


def test_stats_page_renders(client, world):
    response = client.get("/stats")
    assert response.status_code == 200
    assert "Michael Mann" in response.text
    assert "Robert De Niro" in response.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# --- statistics -----------------------------------------------------------
def test_runtime_by_selector(db, world):
    rows = {row.participant: row for row in stats_service.runtime_by_selector(db)}
    # Ann picked Heat (170) and Drive (100).
    assert rows["Ann"].movie_count == 2
    assert rows["Ann"].average_runtime == pytest.approx(135.0)
    assert rows["Ann"].total_runtime == 270
    assert rows["Bob"].average_runtime == pytest.approx(98.0)


def test_picks_by_participant(db, world):
    rows = {row["name"]: row for row in stats_service.picks_by_participant(db)}
    assert rows["Ann"]["count"] == 2
    assert rows["Ann"]["first_round"] == 1
    assert rows["Ann"]["last_round"] == 2
    assert "Dee" not in rows  # in the lineup but has not picked yet


def test_top_directors_and_actors(db, world):
    directors = {d.name: d for d in stats_service.top_directors(db)}
    assert directors["Michael Mann"].movie_count == 1
    assert directors["Michael Mann"].entries == [("Heat", 1995, "Ann")]

    actors = {a.name: a for a in stats_service.top_actors(db)}
    # De Niro is in both Heat (Ann) and Ronin (Cy).
    assert actors["Robert De Niro"].movie_count == 2
    assert sorted(e[2] for e in actors["Robert De Niro"].entries) == ["Ann", "Cy"]


def test_overview_totals(db, world):
    data = stats_service.overview(db)
    assert data["total_rounds"] == 2
    assert data["total_picks"] == 4
    assert data["total_runtime_minutes"] == 170 + 98 + 122 + 100


# --- membership windows ---------------------------------------------------
def test_participant_activity_window(db, world):
    dee = world["dee"]
    assert not dee.is_active_for_round(1)
    assert dee.is_active_for_round(2)

    dee.left_round = 5
    db.flush()
    assert dee.is_active_for_round(5)
    assert not dee.is_active_for_round(6)


def test_current_lineup_respects_join_round(db, world):
    names = [p.name for p in round_service.current_lineup(db, 1)]
    assert names == ["Ann", "Bob", "Cy"]
    names = [p.name for p in round_service.current_lineup(db, 2)]
    assert names == ["Ann", "Bob", "Cy", "Dee"]


# --- admin flows ----------------------------------------------------------
def test_admin_pages_render(client, world):
    assert client.get("/admin/participants").status_code == 200
    assert client.get("/admin/rounds").status_code == 200
    assert client.get(f"/admin/rounds/{world['r2'].id}").status_code == 200


def test_create_and_edit_participant(client, db):
    response = client.post(
        "/admin/participants",
        data={"name": "Zed", "trakt_username": "zed", "joined_round": "3"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    zed = db.query(Participant).filter_by(name="Zed").one()
    assert zed.trakt_username == "zed"
    assert zed.joined_round == 3

    client.post(
        f"/admin/participants/{zed.id}",
        data={"name": "Zed", "trakt_username": "", "joined_round": "3",
              "left_round": "7"},
        follow_redirects=False,
    )
    db.expire_all()
    zed = db.query(Participant).filter_by(name="Zed").one()
    assert zed.left_round == 7
    assert zed.trakt_username is None

    client.post(f"/admin/participants/{zed.id}/delete", follow_redirects=False)
    assert db.query(Participant).filter_by(name="Zed").one_or_none() is None


def test_close_round_opens_the_next_one(client, db, world):
    r2 = world["r2"]
    response = client.post(
        f"/admin/rounds/{r2.id}/close",
        data={
            "discussed_on": "2024-03-01",
            "next_started_on": "2024-03-01",
            "open_next": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    closed = db.query(Round).filter_by(number=2).one()
    assert closed.status == RoundStatus.CLOSED
    assert closed.discussed_on == date(2024, 3, 1)

    new_round = db.query(Round).filter_by(number=3).one()
    assert new_round.status == RoundStatus.OPEN
    # The new round inherits everyone active as of round 3.
    lineup = sorted(p.name for p in round_service.round_lineup(db, new_round))
    assert lineup == ["Ann", "Bob", "Cy", "Dee"]


def test_clear_pick(client, db, world):
    r2, ann = world["r2"], world["ann"]
    assert db.query(Pick).filter_by(round_id=r2.id, participant_id=ann.id).one()

    client.post(
        f"/admin/rounds/{r2.id}/picks/{ann.id}/delete", follow_redirects=False
    )
    db.expire_all()
    assert (
        db.query(Pick).filter_by(round_id=r2.id, participant_id=ann.id).one_or_none()
        is None
    )


def test_tmdb_search_requires_a_key(client):
    # No TMDB_API_KEY is configured in the test environment.
    assert client.get("/api/tmdb/search?q=heat").status_code == 503


def test_a_carried_over_rating_is_marked_in_the_watch_grid(client, db):
    """The Dark City case, end to end: rated years earlier, never re-watched."""
    from datetime import datetime

    from app.models import Rating

    justin = f.make_participant(db, "Justin")
    ryan = f.make_participant(db, "Ryan")
    dark_city = f.make_movie(db, "Dark City", runtime=100, year=1998)

    round_ = f.make_round(db, 76, [justin, ryan], started_on=date(2025, 1, 1))
    f.make_pick(db, round_, justin, dark_city)

    # Ryan rated it in 2019 and has no watch inside the round.
    db.add(Rating(round_id=round_.id, participant_id=ryan.id, movie_id=dark_city.id,
                  rating=8, rated_at=datetime(2019, 3, 4)))
    db.commit()

    body = client.get("/").text
    assert "Dark City" in body
    # The score is shown, flagged as carried over, and explained.
    assert 'class="rating carried"' in body
    assert "Rated Mar 2019, before this round" in body
    assert "before this round began" in body  # the legend


def test_a_rating_made_during_the_round_is_not_marked(client, db):
    from datetime import datetime

    from app.models import Rating

    ann = f.make_participant(db, "Ann")
    movie = f.make_movie(db, "Sicario", year=2015)
    round_ = f.make_round(db, 80, [ann], started_on=date(2025, 1, 1))
    f.make_pick(db, round_, ann, movie)
    db.add(Rating(round_id=round_.id, participant_id=ann.id, movie_id=movie.id,
                  rating=9, rated_at=datetime(2025, 1, 15)))
    db.commit()

    body = client.get("/").text
    assert 'class="rating "' in body or 'class="rating"' in body
    assert "carried" not in body
    assert "before this round began" not in body
