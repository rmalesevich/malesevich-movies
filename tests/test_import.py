"""Historical CSV seeding."""
from datetime import date

import pytest

from app.models import Movie, Participant, Pick, Round, RoundStatus
from app.services import rounds as round_service
from app.services.importer import import_rounds, read_rows

CATALOG = {
    949: {"id": 949, "title": "Heat", "release_date": "1995-12-15", "runtime": 170,
          "overview": "Cops and robbers.", "poster_path": "/heat.jpg",
          "genres": [{"id": 28, "name": "Action"}],
          "credits": {"cast": [{"id": 1, "name": "Al Pacino", "character": "Hanna",
                                "order": 0}],
                      "crew": [{"id": 2, "name": "Michael Mann", "job": "Director",
                                "department": "Directing"}]}},
    275: {"id": 275, "title": "Fargo", "release_date": "1996-03-08", "runtime": 98,
          "overview": "A kidnapping goes wrong.", "poster_path": "/fargo.jpg",
          "genres": [{"id": 80, "name": "Crime"}],
          "credits": {"cast": [], "crew": [{"id": 3, "name": "Joel Coen",
                                            "job": "Director",
                                            "department": "Directing"}]}},
    680: {"id": 680, "title": "Pulp Fiction", "release_date": "1994-09-10",
          "runtime": 154, "overview": "Interlocking stories.",
          "poster_path": "/pf.jpg", "genres": [], "credits": {"cast": [], "crew": []}},
}


class FakeTMDB:
    """Serves the catalog above and records what was searched for."""

    def __init__(self):
        self.searches = []

    def search_movies(self, query, page=1):
        self.searches.append(query)
        return [
            {"id": m["id"], "title": m["title"], "release_date": m["release_date"],
             "popularity": 10.0}
            for m in CATALOG.values()
            if query.lower() in m["title"].lower()
        ]

    def movie(self, tmdb_id):
        if tmdb_id not in CATALOG:
            raise AssertionError(f"unexpected TMDB fetch for {tmdb_id}")
        return CATALOG[tmdb_id]


def write_csv(tmp_path, text):
    path = tmp_path / "rounds.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_import_creates_rounds_participants_and_picks(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,discussed_on,participant,tmdb_id,title,year
1,2022-02-19,2022-03-19,Ryan,949,Heat,1995
1,2022-02-19,2022-03-19,Dad,275,Fargo,1996
2,2022-03-20,2022-04-20,Ryan,680,Pulp Fiction,1994
2,2022-03-20,2022-04-20,Dad,949,Heat,1995
""")

    report = import_rounds(db, path, FakeTMDB())

    assert report.rounds_created == 2
    assert report.participants_created == 2
    assert report.movies_created == 3   # Heat is reused in round 2
    assert report.picks_created == 4

    r1 = db.query(Round).filter_by(number=1).one()
    assert r1.started_on == date(2022, 2, 19)
    assert r1.discussed_on == date(2022, 3, 19)
    assert r1.status == RoundStatus.CLOSED
    assert len(round_service.round_lineup(db, r1)) == 2

    heat = db.query(Movie).filter_by(tmdb_id=949).one()
    assert heat.runtime == 170
    assert heat.title == "Heat"
    # Credits and genres came along with it.
    assert {c.person.name for c in heat.credits} == {"Al Pacino", "Michael Mann"}
    assert [g.genre.name for g in heat.genres] == ["Action"]


def test_participants_join_at_the_round_they_first_appear(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id
1,2022-02-19,Ryan,949
2,2022-03-20,Ryan,275
2,2022-03-20,Newcomer,680
""")

    import_rounds(db, path, FakeTMDB())

    assert db.query(Participant).filter_by(name="Ryan").one().joined_round == 1
    assert db.query(Participant).filter_by(name="Newcomer").one().joined_round == 2


def test_missing_discussion_date_falls_back_to_the_next_round_start(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id
1,2022-02-19,Ryan,949
2,2022-03-20,Ryan,275
""")

    import_rounds(db, path, FakeTMDB())

    r1 = db.query(Round).filter_by(number=1).one()
    assert r1.discussed_on == date(2022, 3, 19)  # day before round 2 opens


def test_titles_are_resolved_when_tmdb_id_is_blank(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id,title,year
1,2022-02-19,Ryan,,Fargo,1996
""")

    client = FakeTMDB()
    report = import_rounds(db, path, client)

    assert client.searches == ["Fargo"]
    assert report.picks_created == 1
    assert db.query(Pick).one().movie.tmdb_id == 275


def test_existing_rounds_are_left_alone(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id
1,2022-02-19,Ryan,949
""")
    import_rounds(db, path, FakeTMDB())
    report = import_rounds(db, path, FakeTMDB())

    assert report.rounds_created == 0
    assert report.picks_created == 0
    assert any("already exists" in note for note in report.skipped)
    assert db.query(Round).count() == 1


def test_dry_run_writes_nothing(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id
1,2022-02-19,Ryan,949
""")

    report = import_rounds(db, path, FakeTMDB(), dry_run=True)

    assert report.rounds_created == 1     # reported...
    assert db.query(Round).count() == 0   # ...but rolled back


def test_a_missing_required_column_is_rejected(tmp_path):
    path = write_csv(tmp_path, "round,started_on,tmdb_id\n1,2022-02-19,949\n")
    with pytest.raises(ValueError, match="participant"):
        read_rows(path)


def test_us_style_dates_are_accepted(db, tmp_path):
    path = write_csv(tmp_path, """round,started_on,participant,tmdb_id
1,02/19/2022,Ryan,949
""")

    import_rounds(db, path, FakeTMDB())

    assert db.query(Round).one().started_on == date(2022, 2, 19)
