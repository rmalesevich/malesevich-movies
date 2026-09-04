"""The masthead tagline: derived from data, or absent entirely."""
from datetime import date

from app.services.rounds import tagline
from tests import factories as f


def rounds_on(db, people, *starts):
    for index, start in enumerate(starts, start=1):
        f.make_round(db, index, people, started_on=start)
    db.flush()


def test_headcount_and_interval(db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob", "Cy", "Dee")]
    # Three gaps of 20, 22 and 21 days -> mean 21.0.
    rounds_on(db, people, date(2024, 1, 1), date(2024, 1, 21),
              date(2024, 2, 12), date(2024, 3, 4))

    assert tagline(db) == "4 people. 4 films. One argument every 21 days."


def test_the_interval_is_rounded_up(db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob", "Cy")]
    # Two gaps totalling 43 days -> mean 21.5 -> 22.
    rounds_on(db, people, date(2024, 1, 1), date(2024, 1, 22), date(2024, 2, 13))

    assert "every 22 days" in tagline(db)


def test_departed_participants_are_not_counted(db):
    ann = f.make_participant(db, "Ann")
    f.make_participant(db, "Bob")
    f.make_participant(db, "Gone", joined_round=1)
    db.query(type(ann)).filter_by(name="Gone").one().left_round = 4
    rounds_on(db, [ann], date(2024, 1, 1), date(2024, 1, 15))
    db.flush()

    assert tagline(db).startswith("2 people. 2 films.")


def test_a_single_participant_reads_grammatically(db):
    solo = f.make_participant(db, "Ann")
    rounds_on(db, [solo], date(2024, 1, 1), date(2024, 1, 8))

    assert tagline(db) == "1 person. 1 film. One argument every 7 days."


def test_a_one_day_interval_drops_the_number(db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob")]
    rounds_on(db, people, date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3))

    assert tagline(db) == "2 people. 2 films. One argument every day."


# --- cases where nothing should be printed --------------------------------
def test_empty_database_has_no_tagline(db):
    assert tagline(db) is None


def test_no_active_participants_has_no_tagline(db):
    ann = f.make_participant(db, "Ann")
    ann.left_round = 2
    rounds_on(db, [ann], date(2024, 1, 1), date(2024, 1, 22))
    db.flush()

    assert tagline(db) is None


def test_a_single_round_has_no_measurable_interval(db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob")]
    rounds_on(db, people, date(2024, 1, 1))

    assert tagline(db) is None


def test_participants_but_no_rounds_has_no_tagline(db):
    f.make_participant(db, "Ann")
    db.flush()

    assert tagline(db) is None


def test_rounds_sharing_one_start_date_have_no_interval(db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob")]
    rounds_on(db, people, date(2024, 1, 1), date(2024, 1, 1))

    assert tagline(db) is None


# --- rendering ------------------------------------------------------------
def test_the_tagline_renders_in_the_masthead(client, db):
    people = [f.make_participant(db, n) for n in ("Ann", "Bob", "Cy", "Dee")]
    rounds_on(db, people, date(2024, 1, 1), date(2024, 1, 23))
    db.commit()

    body = client.get("/").text
    expected = "4 people. 4 films. One argument every 22 days."
    assert f'<span class="tagline">{expected}</span>' in body


def test_no_tagline_element_when_it_cannot_be_derived(client, db):
    """An empty install shows no half-filled sentence."""
    body = client.get("/").text

    assert 'class="tagline"' not in body
    assert "argument" not in body
    # The rest of the masthead is still intact.
    assert "Malesevich Movies" in body
