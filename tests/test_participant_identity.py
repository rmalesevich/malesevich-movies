"""Name matching: 'ryan', 'Ryan' and 'Ryan  M' are not four different people."""
from datetime import date

import pytest

from app.models import Participant, Pick, Rating, Watch
from app.services.participants import (
    clean_name,
    find_by_name,
    merge,
    normalize_name,
)
from tests import factories as f


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ryan", "ryan"),
        ("ryan", "ryan"),
        ("  Ryan  ", "ryan"),
        ("RYAN", "ryan"),
        ("Ryan  Malesevich", "ryan malesevich"),
        ("Ryan\tMalesevich", "ryan malesevich"),
        ("ryan malesevich", "ryan malesevich"),
    ],
)
def test_names_normalize_to_one_key(raw, expected):
    assert normalize_name(raw) == expected


def test_clean_name_keeps_capitalisation_but_fixes_spacing():
    # Storage keeps how the person spelled it; only stray whitespace is removed.
    assert clean_name("  Ryan   Malesevich ") == "Ryan Malesevich"
    assert clean_name("ryan") == "ryan"
    assert clean_name("McDonald") == "McDonald"


def test_lookup_ignores_case_and_spacing(db):
    f.make_participant(db, "Ryan")
    db.flush()

    for spelling in ("ryan", "RYAN", "  Ryan  ", "rYaN"):
        found = find_by_name(db, spelling)
        assert found is not None, spelling
        assert found.name == "Ryan"


def test_lookup_still_distinguishes_different_people(db):
    f.make_participant(db, "Ryan")
    f.make_participant(db, "Ryanne")
    db.flush()

    assert find_by_name(db, "ryan").name == "Ryan"
    assert find_by_name(db, "RYANNE").name == "Ryanne"
    assert find_by_name(db, "Bob") is None


# --- merging duplicates created before matching was loosened ---------------
def test_merge_moves_picks_and_history(db):
    upper = f.make_participant(db, "Ryan", joined_round=1)
    lower = f.make_participant(db, "ryan", joined_round=3)
    movie_a = f.make_movie(db, "Heat")
    movie_b = f.make_movie(db, "Fargo")

    r1 = f.make_round(db, 1, [upper], started_on=date(2024, 1, 1))
    f.make_pick(db, r1, upper, movie_a)
    r2 = f.make_round(db, 2, [lower], started_on=date(2024, 2, 1))
    f.make_pick(db, r2, lower, movie_b)
    f.make_watch(db, r2, lower, movie_b)
    f.make_rating(db, r2, lower, movie_b, score=9)
    db.commit()

    moved = merge(db, lower, upper)
    db.commit()

    assert db.query(Participant).count() == 1
    kept = db.query(Participant).one()
    assert kept.name == "Ryan"
    assert kept.joined_round == 1                       # widest window kept
    assert moved == {"picks": 1, "watches": 1, "ratings": 1, "rounds": 1,
                     "dropped": 0}
    assert {p.participant_id for p in db.query(Pick).all()} == {kept.id}
    assert db.query(Watch).one().participant_id == kept.id
    assert db.query(Rating).one().participant_id == kept.id


def test_merge_drops_rows_that_would_collide(db):
    """Both records picking in the same round cannot both survive."""
    upper = f.make_participant(db, "Ryan")
    lower = f.make_participant(db, "ryan")
    movie = f.make_movie(db, "Heat")

    r1 = f.make_round(db, 1, [upper, lower], started_on=date(2024, 1, 1))
    f.make_pick(db, r1, upper, movie)
    f.make_pick(db, r1, lower, movie)
    db.commit()

    moved = merge(db, lower, upper)
    db.commit()

    assert db.query(Pick).count() == 1
    assert db.query(Pick).one().participant_id == upper.id
    assert moved["dropped"] == 2   # the colliding pick and round membership


def test_merge_carries_over_a_trakt_username(db):
    plain = f.make_participant(db, "Ryan")
    with_trakt = f.make_participant(db, "ryan", trakt_username="ryanm")
    db.commit()

    merge(db, with_trakt, plain)
    db.commit()

    assert db.query(Participant).one().trakt_username == "ryanm"


def test_existing_duplicates_stay_addressable_by_exact_name(db):
    """Without this, a pre-existing duplicate could never be merged."""
    from app.services.participants import resolve

    upper = f.make_participant(db, "Ryan", joined_round=1)
    lower = f.make_participant(db, "ryan", joined_round=3)
    db.flush()

    # A loose lookup collapses them onto one record...
    assert find_by_name(db, "RYAN").id == upper.id
    assert find_by_name(db, "ryan").id == upper.id
    # ...but an exact spelling still reaches each one.
    assert resolve(db, "Ryan").id == upper.id
    assert resolve(db, "ryan").id == lower.id
    # A spelling matching neither exactly still falls back to loose matching.
    assert resolve(db, "  RyAn ").id == upper.id


def test_the_index_prefers_the_earliest_record_on_a_duplicate(db):
    from app.services.participants import name_index

    first = f.make_participant(db, "Ryan")
    f.make_participant(db, "ryan")
    db.flush()

    assert name_index(db)["ryan"].id == first.id
