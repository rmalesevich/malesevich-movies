"""Hand-written film notes: storage, Markdown rendering, and the two pages."""
from datetime import date

import pytest

from app.models import MovieNote, RoundStatus
from app.services import notes as notes_service
from tests import factories as f


@pytest.fixture
def world(db):
    """Two rounds, three films. Heat is picked twice, by two people.

    That repeat is the case that decides whether a note belongs to the film or
    to the pick - here it belongs to the film, so one note covers both rounds.
    """
    ann = f.make_participant(db, "Ann", joined_round=1)
    bob = f.make_participant(db, "Bob", joined_round=1)

    heat = f.make_movie(db, "Heat", runtime=170, year=1995)
    fargo = f.make_movie(db, "Fargo", runtime=98, year=1996)

    r1 = f.make_round(db, 1, [ann, bob], status=RoundStatus.CLOSED,
                      started_on=date(2024, 1, 1), discussed_on=date(2024, 2, 1))
    f.make_pick(db, r1, ann, heat)
    f.make_pick(db, r1, bob, fargo)

    r2 = f.make_round(db, 2, [ann, bob], started_on=date(2024, 2, 2))
    f.make_pick(db, r2, bob, heat)

    db.commit()
    return {"ann": ann, "bob": bob, "heat": heat, "fargo": fargo, "r1": r1, "r2": r2}


# --- rendering ------------------------------------------------------------
def test_markdown_becomes_html():
    html = notes_service.render_markdown("# Heat\n\nA **great** film.")
    assert "<h1>Heat</h1>" in html
    assert "<strong>great</strong>" in html


def test_tables_and_strikethrough_are_enabled():
    html = notes_service.render_markdown(
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n~~dropped~~"
    )
    assert "<table>" in html and "<th>a</th>" in html
    assert "<s>dropped</s>" in html


def test_raw_html_in_the_source_is_escaped_not_executed():
    html = notes_service.render_markdown("Watch out <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_javascript_url_does_not_become_a_link():
    # markdown-it's link validator refuses the scheme outright, so the source
    # is left as literal text rather than rendered into an anchor.
    html = notes_service.render_markdown("[click](javascript:alert(1))")
    assert "<a " not in html
    assert "href" not in html


def test_rendering_nothing_is_not_an_error():
    assert notes_service.render_markdown(None) == ""
    assert notes_service.render_markdown("   ") == ""


# --- storage --------------------------------------------------------------
def test_saving_then_reading_back(db, world):
    notes_service.save_note(db, world["heat"], "  Pacino and De Niro.  ")
    db.commit()

    note = notes_service.get_note(db, world["heat"].id)
    # Stored stripped, and as Markdown source rather than rendered HTML.
    assert note.body == "Pacino and De Niro."


def test_saving_again_updates_in_place(db, world):
    notes_service.save_note(db, world["heat"], "First pass.")
    db.commit()
    notes_service.save_note(db, world["heat"], "Second pass.")
    db.commit()

    assert db.query(MovieNote).count() == 1
    assert notes_service.get_note(db, world["heat"].id).body == "Second pass."


def test_a_blank_body_clears_the_note(db, world):
    notes_service.save_note(db, world["heat"], "Something.")
    db.commit()
    assert notes_service.save_note(db, world["heat"], "   \n  ") is None
    db.commit()
    assert notes_service.get_note(db, world["heat"].id) is None


def test_a_blank_body_on_a_film_with_no_note_is_a_no_op(db, world):
    assert notes_service.save_note(db, world["heat"], "") is None
    db.commit()
    assert db.query(MovieNote).count() == 0


def test_delete_reports_whether_there_was_anything_to_delete(db, world):
    assert notes_service.delete_note(db, world["heat"]) is False
    notes_service.save_note(db, world["heat"], "Something.")
    db.commit()
    assert notes_service.delete_note(db, world["heat"]) is True
    db.commit()
    assert db.query(MovieNote).count() == 0


def test_ids_with_notes_is_one_lookup_for_a_whole_round(db, world):
    notes_service.save_note(db, world["heat"], "Only Heat.")
    db.commit()

    ids = [world["heat"].id, world["fargo"].id]
    assert notes_service.movie_ids_with_notes(db, ids) == {world["heat"].id}
    assert notes_service.movie_ids_with_notes(db, []) == set()


def test_the_note_goes_when_the_film_does(db):
    movie = f.make_movie(db, "Sorcerer", year=1977)
    notes_service.save_note(db, movie, "Bridge sequence.")
    db.commit()
    db.delete(movie)
    db.commit()
    assert db.query(MovieNote).count() == 0


# --- the film profile on the page ----------------------------------------
def test_profile_lists_the_rounds_a_film_was_picked_in(db, world):
    profile = notes_service.movie_profile(db, world["heat"])
    assert [(a.round_number, a.selector) for a in profile.appearances] == [
        (1, "Ann"),
        (2, "Bob"),
    ]


def test_profile_groups_ratings_by_round(db, world):
    f.make_rating(db, world["r1"], world["ann"], world["heat"], score=9)
    f.make_rating(db, world["r1"], world["bob"], world["heat"], score=7)
    f.make_rating(db, world["r2"], world["bob"], world["heat"], score=6)
    db.commit()

    first, second = notes_service.movie_profile(db, world["heat"]).appearances
    assert first.ratings == [("Ann", 9), ("Bob", 7)]
    assert first.average == 8
    assert second.ratings == [("Bob", 6)]


def test_profile_resolves_director_and_billed_cast_in_order(db, world):
    from app.models import CreditKind

    heat = world["heat"]
    f.credit(db, heat, "Michael Mann", CreditKind.CREW, job="Director")
    f.credit(db, heat, "Al Pacino", CreditKind.CAST, order=1)
    f.credit(db, heat, "Robert De Niro", CreditKind.CAST, order=0)
    db.commit()

    profile = notes_service.movie_profile(db, heat)
    assert profile.directors == ["Michael Mann"]
    assert profile.cast == ["Robert De Niro", "Al Pacino"]
    assert profile.runtime_display == "2h 50m"


def test_runtime_display_omits_a_zero_hour(db):
    movie = f.make_movie(db, "Short", runtime=42)
    db.commit()
    assert notes_service.movie_profile(db, movie).runtime_display == "42m"


# --- pages ----------------------------------------------------------------
def test_round_page_invites_notes_when_there_are_none(client, world):
    body = client.get("/rounds/1").text
    assert f'href="/movies/{world["heat"].id}/notes"' in body
    assert "Add notes" in body
    assert "note-link has-note" not in body


def test_round_page_marks_a_film_that_has_notes(client, db, world):
    notes_service.save_note(db, world["heat"], "Written up.")
    db.commit()

    body = client.get("/rounds/1").text
    assert "note-link has-note" in body
    # Fargo has nothing written about it, so it still shows the invitation.
    assert "note-link no-note" in body


def test_the_indicator_follows_the_film_into_its_other_round(client, db, world):
    notes_service.save_note(db, world["heat"], "Written up.")
    db.commit()
    assert "note-link has-note" in client.get("/rounds/2").text


def test_notes_page_renders_the_markdown_and_the_film_details(client, db, world):
    from app.models import CreditKind

    f.credit(db, world["heat"], "Michael Mann", CreditKind.CREW, job="Director")
    notes_service.save_note(db, world["heat"], "## The heist\n\nBest **shootout**.")
    db.commit()

    body = client.get(f"/movies/{world['heat'].id}/notes").text
    assert "<h2>The heist</h2>" in body
    assert "<strong>shootout</strong>" in body
    assert "Michael Mann" in body
    assert "Heat" in body and "1995" in body
    assert 'href="/rounds/1"' in body


def test_a_film_with_no_notes_lands_on_the_form(client, world):
    resp = client.get(f"/movies/{world['heat'].id}/notes", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/movies/{world['heat'].id}/notes/edit"

    form = client.get(f"/movies/{world['heat'].id}/notes/edit").text
    assert '<textarea id="body"' in form
    assert "Add notes" in form


def test_the_form_comes_back_prefilled_with_the_markdown_source(client, db, world):
    notes_service.save_note(db, world["heat"], "# Kept as written")
    db.commit()

    form = client.get(f"/movies/{world['heat'].id}/notes/edit").text
    assert "# Kept as written" in form
    assert "Save notes" in form


def test_saving_through_the_form_lands_on_the_notes_page(client, db, world):
    heat = world["heat"]
    resp = client.post(
        f"/movies/{heat.id}/notes", data={"body": "Typed in the box."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/movies/{heat.id}/notes"
    assert notes_service.get_note(db, heat.id).body == "Typed in the box."


def test_saving_an_empty_box_clears_and_returns_to_the_form(client, db, world):
    heat = world["heat"]
    client.post(f"/movies/{heat.id}/notes", data={"body": "Something."})
    resp = client.post(
        f"/movies/{heat.id}/notes", data={"body": ""}, follow_redirects=False
    )
    assert resp.headers["location"] == f"/movies/{heat.id}/notes/edit"
    assert notes_service.get_note(db, heat.id) is None


def test_deleting_through_the_form(client, db, world):
    heat = world["heat"]
    client.post(f"/movies/{heat.id}/notes", data={"body": "Something."})
    resp = client.post(f"/movies/{heat.id}/notes/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(MovieNote).count() == 0


def test_cancel_goes_back_to_the_latest_round_the_film_was_in(client, world):
    # Heat was picked in rounds 1 and 2, so the form offers round 2.
    form = client.get(f"/movies/{world['heat'].id}/notes/edit").text
    assert 'href="/rounds/2"' in form


def test_an_unknown_film_is_a_404(client, world):
    assert client.get("/movies/9999/notes").status_code == 404
    assert client.get("/movies/9999/notes/edit").status_code == 404
    assert client.post("/movies/9999/notes", data={"body": "x"}).status_code == 404
