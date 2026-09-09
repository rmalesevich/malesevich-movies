"""The Physical Media shelf: ownership records, counts and page."""
from datetime import date

import pytest

from app.models import MediaFormat, PhysicalCopy, RoundStatus
from app.services import physical as shelf_service
from tests import factories as f


@pytest.fixture
def shelf_world(db):
    """Two rounds, four picks, three distinct films.

    Heat is picked twice, by different people in different rounds - the case
    that separates "distinct titles" from "picks".
    """
    ann = f.make_participant(db, "Ann", joined_round=1)
    bob = f.make_participant(db, "Bob", joined_round=1)

    heat = f.make_movie(db, "Heat", runtime=170, year=1995)
    fargo = f.make_movie(db, "Fargo", runtime=98, year=1996)
    drive = f.make_movie(db, "Drive", runtime=100, year=2011)

    r1 = f.make_round(db, 1, [ann, bob], status=RoundStatus.CLOSED,
                      started_on=date(2024, 1, 1), discussed_on=date(2024, 2, 1))
    f.make_pick(db, r1, ann, heat)
    f.make_pick(db, r1, bob, fargo)

    r2 = f.make_round(db, 2, [ann, bob], started_on=date(2024, 2, 2))
    f.make_pick(db, r2, ann, drive)
    f.make_pick(db, r2, bob, heat)

    db.commit()
    return {"heat": heat, "fargo": fargo, "drive": drive}


def own(db, movie, *formats):
    for fmt in formats:
        db.add(PhysicalCopy(movie_id=movie.id, format=fmt))
    db.commit()


# --- service --------------------------------------------------------------
def test_shelf_lists_distinct_titles_alphabetically(db, shelf_world):
    entries = shelf_service.shelf(db)
    assert [e.movie.title for e in entries] == ["Drive", "Fargo", "Heat"]


def test_shelf_ignores_leading_articles_when_alphabetising(db, shelf_world):
    # One pick each - picks are unique per participant per round.
    pickers = [f.make_participant(db, name, joined_round=3)
               for name in ("Cal", "Dee", "Eve")]
    r3 = f.make_round(db, 3, pickers, started_on=date(2024, 3, 1))
    for picker, title in zip(pickers, ("The Godfather", "An Education", "A Prophet")):
        f.make_pick(db, r3, picker, f.make_movie(db, title))
    db.commit()

    titles = [e.movie.title for e in shelf_service.shelf(db)]
    assert titles == [
        "Drive",
        "An Education",
        "Fargo",
        "The Godfather",
        "Heat",
        "A Prophet",
    ]


def test_sort_key_keeps_a_title_that_is_only_an_article():
    assert shelf_service.sort_key("The") == "the"
    assert shelf_service.sort_key("Theodore") == "theodore"
    assert shelf_service.sort_key("A Prophet") == "prophet"


def test_shelf_collapses_a_film_picked_in_two_rounds(db, shelf_world):
    heat = next(e for e in shelf_service.shelf(db) if e.movie.title == "Heat")
    assert heat.rounds == [1, 2]
    assert heat.selectors == ["Ann", "Bob"]


def test_unowned_film_has_no_formats(db, shelf_world):
    entries = {e.movie.title: e for e in shelf_service.shelf(db)}
    assert entries["Heat"].owned is False
    assert entries["Heat"].best_format is None


def test_formats_are_ordered_best_first(db, shelf_world):
    # Deliberately inserted worst-first; the shelf should still lead with 4K.
    own(db, shelf_world["heat"], MediaFormat.DVD, MediaFormat.UHD_4K,
        MediaFormat.BLU_RAY)
    heat = next(e for e in shelf_service.shelf(db) if e.movie.title == "Heat")
    assert heat.formats == [MediaFormat.UHD_4K, MediaFormat.BLU_RAY, MediaFormat.DVD]
    assert heat.best_format is MediaFormat.UHD_4K


def test_overview_counts_titles_once_and_discs_per_format(db, shelf_world):
    own(db, shelf_world["heat"], MediaFormat.UHD_4K, MediaFormat.BLU_RAY)
    own(db, shelf_world["fargo"], MediaFormat.DVD)

    overview = shelf_service.overview(db)
    assert overview.total_rounds == 2
    assert overview.distinct_titles == 3
    # Heat counts once as a title even though it is two discs.
    assert overview.titles_owned == 2
    assert overview.titles_missing == 1
    assert overview.by_format == {
        MediaFormat.UHD_4K: 1,
        MediaFormat.BLU_RAY: 1,
        MediaFormat.DVD: 1,
    }


def test_overview_on_an_empty_database(db):
    overview = shelf_service.overview(db)
    assert overview.distinct_titles == 0
    assert overview.titles_owned == 0
    assert overview.owned_percent == 0.0


def test_toggle_adds_then_removes(db, shelf_world):
    heat = shelf_world["heat"]
    assert shelf_service.toggle_format(db, heat, MediaFormat.UHD_4K) is True
    db.commit()
    assert shelf_service.overview(db).titles_owned == 1

    assert shelf_service.toggle_format(db, heat, MediaFormat.UHD_4K) is False
    db.commit()
    assert shelf_service.overview(db).titles_owned == 0


def test_copies_go_when_the_movie_does(db):
    # Stands alone rather than using the fixture: a picked film cannot be
    # deleted without clearing its picks first, which is a separate concern.
    movie = f.make_movie(db, "Sorcerer", year=1977)
    own(db, movie, MediaFormat.DVD, MediaFormat.BLU_RAY)
    assert db.query(PhysicalCopy).count() == 2

    db.delete(movie)
    db.commit()
    assert db.query(PhysicalCopy).count() == 0


# --- page -----------------------------------------------------------------
def test_page_shows_stats_and_posters(client, db, shelf_world):
    own(db, shelf_world["heat"], MediaFormat.UHD_4K)

    body = client.get("/physical-media").text
    assert "Physical Media" in body
    assert "Titles owned" in body
    assert "Distinct titles" in body
    assert 'class="fmt-badge fmt-4k">4K<' in body
    # Unowned titles are rendered too, just faded by the card's class.
    assert "shelf-card unowned" in body
    assert "shelf-card owned" in body


def test_filters_narrow_the_grid(client, db, shelf_world):
    own(db, shelf_world["heat"], MediaFormat.BLU_RAY)

    owned = client.get("/physical-media?show=owned").text
    assert "Heat" in owned and "Fargo" not in owned

    missing = client.get("/physical-media?show=missing").text
    assert "Fargo" in missing and ">Heat<" not in missing


def test_unknown_filter_falls_back_to_all(client, shelf_world):
    body = client.get("/physical-media?show=nonsense").text
    assert "3 titles shown" in body


def test_toggle_round_trip_through_the_form(client, db, shelf_world):
    heat = shelf_world["heat"]
    resp = client.post(
        f"/physical-media/{heat.id}/toggle",
        data={"format": "4k", "show": "all"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"#movie-{heat.id}")
    assert db.query(PhysicalCopy).count() == 1

    client.post(f"/physical-media/{heat.id}/toggle",
                data={"format": "4k", "show": "all"}, follow_redirects=False)
    assert db.query(PhysicalCopy).count() == 0


def test_toggle_rejects_an_unknown_format(client, shelf_world):
    heat = shelf_world["heat"]
    assert client.post(f"/physical-media/{heat.id}/toggle",
                       data={"format": "laserdisc"}).status_code == 400


def test_toggle_rejects_an_unknown_movie(client, shelf_world):
    assert client.post("/physical-media/9999/toggle",
                       data={"format": "dvd"}).status_code == 404


def test_nav_link_is_in_the_main_menu(client, shelf_world):
    assert 'href="/physical-media"' in client.get("/").text
