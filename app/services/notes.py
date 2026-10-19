"""Hand-written film notes: Markdown storage, rendering and film profiles.

Notes are keyed by film, not by pick - see ``MovieNote`` for why - and there
is a single author, so nothing here deals with attribution.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Credit,
    CreditKind,
    Genre,
    Movie,
    MovieGenre,
    MovieNote,
    Participant,
    Person,
    Pick,
    Rating,
    Round,
)

# CommonMark plus the two GitHub extensions actually used when writing about a
# film: tables, and strikethrough. ``html=False`` escapes any raw HTML in the
# source instead of passing it through, which keeps the rendered page safe
# without needing a separate sanitiser pass. markdown-it's own link validator
# rejects javascript:, vbscript: and data: URLs.
_MD = MarkdownIt("commonmark", {"html": False, "typographer": True}).enable(
    ["table", "strikethrough", "replacements", "smartquotes"]
)

# How many billed cast members the profile lists before it stops being useful.
PROFILE_CAST_LIMIT = 8


def render_markdown(body: str | None) -> Markup:
    """Markdown source to HTML, ready to drop into a template unescaped."""
    # Trusted by construction: html=False above escapes any raw HTML in the
    # source, so the only tags left are the ones markdown-it emitted itself.
    return Markup(_MD.render(body or ""))


def get_note(db: Session, movie_id: int) -> MovieNote | None:
    return db.scalar(select(MovieNote).where(MovieNote.movie_id == movie_id))


def movie_ids_with_notes(db: Session, movie_ids: list[int]) -> set[int]:
    """Which of these films have been written about.

    One query for a whole round, so the film rows can show the indicator
    without a lookup each.
    """
    if not movie_ids:
        return set()
    return set(
        db.scalars(
            select(MovieNote.movie_id).where(MovieNote.movie_id.in_(movie_ids))
        ).all()
    )


def save_note(db: Session, movie: Movie, body: str) -> MovieNote | None:
    """Create, update or clear the note on one film.

    A body that is blank once stripped deletes the note rather than storing an
    empty one, so "has notes" stays a question the data can answer honestly.
    Returns the stored note, or None if it was cleared. The caller commits.
    """
    body = (body or "").strip()
    note = get_note(db, movie.id)

    if not body:
        if note is not None:
            db.delete(note)
        return None

    if note is None:
        note = MovieNote(movie_id=movie.id, body=body)
        db.add(note)
    else:
        note.body = body
    db.flush()
    return note


def delete_note(db: Session, movie: Movie) -> bool:
    """Remove the note on one film. Returns True if there was one."""
    note = get_note(db, movie.id)
    if note is None:
        return False
    db.delete(note)
    return True


@dataclass
class Appearance:
    """One time this film turned up in a round, and what it scored."""

    round_number: int
    round_id: int
    selector: str
    # Every rating logged for the film in that round, best first.
    ratings: list[tuple[str, int]] = field(default_factory=list)

    @property
    def average(self) -> float | None:
        if not self.ratings:
            return None
        return sum(score for _, score in self.ratings) / len(self.ratings)


@dataclass
class MovieProfile:
    """Everything the notes page says about a film besides the notes."""

    movie: Movie
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    appearances: list[Appearance] = field(default_factory=list)

    @property
    def runtime_display(self) -> str | None:
        minutes = self.movie.runtime
        if not minutes:
            return None
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m" if hours else f"{mins}m"


def movie_profile(db: Session, movie: Movie) -> MovieProfile:
    """Assemble the film's details for the notes page."""
    credits_ = db.execute(
        select(Credit.kind, Credit.job, Credit.billing_order, Person.name)
        .join(Person, Person.id == Credit.person_id)
        .where(Credit.movie_id == movie.id)
    ).all()

    directors: list[str] = []
    billed: list[tuple[int, str]] = []
    for kind, job, order, name in credits_:
        if kind == CreditKind.CREW and job == "Director":
            if name not in directors:
                directors.append(name)
        elif kind == CreditKind.CAST:
            billed.append((order if order is not None else 10_000, name))

    cast: list[str] = []
    for _, name in sorted(billed):
        if name not in cast:
            cast.append(name)

    genres = list(
        db.scalars(
            select(Genre.name)
            .join(MovieGenre, MovieGenre.genre_id == Genre.id)
            .where(MovieGenre.movie_id == movie.id)
            .order_by(Genre.name)
        ).all()
    )

    appearances = [
        Appearance(round_number=number, round_id=round_id, selector=selector)
        for number, round_id, selector in db.execute(
            select(Round.number, Round.id, Participant.name)
            .join(Pick, Pick.round_id == Round.id)
            .join(Participant, Participant.id == Pick.participant_id)
            .where(Pick.movie_id == movie.id)
            .order_by(Round.number)
        ).all()
    ]
    by_round = {a.round_number: a for a in appearances}

    for number, name, score in db.execute(
        select(Round.number, Participant.name, Rating.rating)
        .join(Round, Round.id == Rating.round_id)
        .join(Participant, Participant.id == Rating.participant_id)
        .where(Rating.movie_id == movie.id)
    ).all():
        appearance = by_round.get(number)
        if appearance is not None:
            appearance.ratings.append((name, score))

    for appearance in appearances:
        appearance.ratings.sort(key=lambda pair: (-pair[1], pair[0]))

    return MovieProfile(
        movie=movie,
        directors=directors,
        cast=cast[:PROFILE_CAST_LIMIT],
        genres=genres,
        appearances=appearances,
    )
