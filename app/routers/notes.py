"""Film notes: read the write-up, or write one.

`/movies/{id}/notes` is the one link worth remembering. It shows the note when
there is one and sends you to the form when there is not, so following it from
a round never dead-ends on an empty page.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Movie
from app.services import notes as notes_service
from app.templating import flash, render

router = APIRouter(prefix="/movies")


def _redirect(url: str) -> RedirectResponse:
    # 303 so the browser turns the POST into a GET.
    return RedirectResponse(url, status_code=303)


def _get_movie(db: Session, movie_id: int) -> Movie:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie


def _back_to_round(profile: notes_service.MovieProfile) -> str:
    """Where "cancel" goes: the latest round the film was picked in.

    A film is only reachable here through a round, so there is almost always
    one to return to; a film with none left (its picks deleted) falls back to
    the archive.
    """
    if profile.appearances:
        return f"/rounds/{profile.appearances[-1].round_number}"
    return "/rounds"


@router.get("/{movie_id}/notes")
def notes_page(movie_id: int, request: Request, db: Session = Depends(get_db)):
    movie = _get_movie(db, movie_id)
    note = notes_service.get_note(db, movie.id)
    if note is None:
        return _redirect(f"/movies/{movie.id}/notes/edit")

    profile = notes_service.movie_profile(db, movie)
    return render(
        request,
        "movie_notes.html",
        {
            "nav": "rounds",
            "movie": movie,
            "note": note,
            "note_html": notes_service.render_markdown(note.body),
            "profile": profile,
            "back": _back_to_round(profile),
        },
    )


@router.get("/{movie_id}/notes/edit")
def notes_form(movie_id: int, request: Request, db: Session = Depends(get_db)):
    movie = _get_movie(db, movie_id)
    note = notes_service.get_note(db, movie.id)
    profile = notes_service.movie_profile(db, movie)
    return render(
        request,
        "movie_notes_edit.html",
        {
            "nav": "rounds",
            "movie": movie,
            "note": note,
            "profile": profile,
            "back": (
                f"/movies/{movie.id}/notes" if note else _back_to_round(profile)
            ),
        },
    )


@router.post("/{movie_id}/notes")
def save_notes(
    movie_id: int,
    request: Request,
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    movie = _get_movie(db, movie_id)
    note = notes_service.save_note(db, movie, body)
    db.commit()

    if note is None:
        # Clearing the textarea and saving is how a note gets removed.
        flash(request, f"Notes on {movie.title} cleared.")
        return _redirect(f"/movies/{movie.id}/notes/edit")

    flash(request, f"Saved notes on {movie.title}.")
    return _redirect(f"/movies/{movie.id}/notes")


@router.post("/{movie_id}/notes/delete")
def delete_notes(movie_id: int, request: Request, db: Session = Depends(get_db)):
    movie = _get_movie(db, movie_id)
    if notes_service.delete_note(db, movie):
        db.commit()
        flash(request, f"Deleted notes on {movie.title}.")
    return _redirect(f"/movies/{movie.id}/notes/edit")
