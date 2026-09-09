"""The physical media shelf: which discussed films are owned, on what disc.

The goal this serves is completionist - eventually own a physical copy of
every film the group has ever discussed - so the unit of interest is the
*distinct* film, not the pick. A film picked twice in different rounds is one
thing to buy, and counts once here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    MediaFormat,
    Movie,
    Participant,
    PhysicalCopy,
    Pick,
    Round,
)

# Display order for the shelf, mirroring MediaFormat's own ordering.
FORMATS: tuple[MediaFormat, ...] = tuple(MediaFormat)

_FORMAT_RANK = {fmt: index for index, fmt in enumerate(FORMATS)}


@dataclass
class ShelfEntry:
    """One distinct film, with everything the card needs to render."""

    movie: Movie
    formats: list[MediaFormat] = field(default_factory=list)
    # Every round the film turned up in, and who picked it, oldest first.
    rounds: list[int] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)

    @property
    def owned(self) -> bool:
        return bool(self.formats)

    @property
    def best_format(self) -> MediaFormat | None:
        """The nicest copy on the shelf - what the poster badge leads with."""
        return self.formats[0] if self.formats else None

    def has(self, fmt: MediaFormat) -> bool:
        return fmt in self.formats


@dataclass
class ShelfOverview:
    total_rounds: int
    distinct_titles: int
    titles_owned: int
    by_format: dict[MediaFormat, int]

    @property
    def titles_missing(self) -> int:
        return self.distinct_titles - self.titles_owned

    @property
    def owned_percent(self) -> float:
        if not self.distinct_titles:
            return 0.0
        return self.titles_owned / self.distinct_titles * 100


def _sorted_formats(formats: list[MediaFormat]) -> list[MediaFormat]:
    return sorted(formats, key=lambda f: _FORMAT_RANK[f])


def shelf(db: Session) -> list[ShelfEntry]:
    """Every distinct picked film, alphabetical, with its discs attached."""
    movies = db.scalars(
        select(Movie)
        .join(Pick, Pick.movie_id == Movie.id)
        .group_by(Movie.id)
        .order_by(func.lower(Movie.title))
    ).all()
    if not movies:
        return []

    entries = {movie.id: ShelfEntry(movie=movie) for movie in movies}

    for movie_id, fmt in db.execute(
        select(PhysicalCopy.movie_id, PhysicalCopy.format)
    ).all():
        entry = entries.get(movie_id)
        if entry is not None:
            entry.formats.append(fmt)

    # One pass for provenance: a film can have been picked more than once, by
    # different people, so both lists are deduplicated in round order.
    for movie_id, number, selector in db.execute(
        select(Pick.movie_id, Round.number, Participant.name)
        .join(Round, Round.id == Pick.round_id)
        .join(Participant, Participant.id == Pick.participant_id)
        .order_by(Round.number)
    ).all():
        entry = entries.get(movie_id)
        if entry is None:
            continue
        if number not in entry.rounds:
            entry.rounds.append(number)
        if selector not in entry.selectors:
            entry.selectors.append(selector)

    for entry in entries.values():
        entry.formats = _sorted_formats(entry.formats)

    return [entries[movie.id] for movie in movies]


def overview(db: Session, entries: list[ShelfEntry] | None = None) -> ShelfOverview:
    """Headline counts. Reuses an already-built shelf when the page has one."""
    entries = shelf(db) if entries is None else entries
    by_format = {fmt: 0 for fmt in FORMATS}
    owned = 0
    for entry in entries:
        if entry.owned:
            owned += 1
        for fmt in entry.formats:
            by_format[fmt] += 1

    return ShelfOverview(
        total_rounds=db.scalar(select(func.count(Round.id))) or 0,
        distinct_titles=len(entries),
        titles_owned=owned,
        by_format=by_format,
    )


def toggle_format(db: Session, movie: Movie, fmt: MediaFormat) -> bool:
    """Add or remove one disc. Returns True if the film is now owned on it."""
    existing = db.scalar(
        select(PhysicalCopy).where(
            PhysicalCopy.movie_id == movie.id, PhysicalCopy.format == fmt
        )
    )
    if existing is not None:
        db.delete(existing)
        return False
    db.add(PhysicalCopy(movie_id=movie.id, format=fmt))
    return True
