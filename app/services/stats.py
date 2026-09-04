"""Aggregate queries behind the statistics page."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.models import (
    Credit,
    CreditKind,
    Genre,
    Movie,
    MovieGenre,
    Participant,
    Person,
    Pick,
    Round,
    RoundStatus,
)

DIRECTOR_JOBS = ("Director",)


@dataclass
class SelectorRuntime:
    participant: str
    movie_count: int
    average_runtime: float | None
    total_runtime: int

    @property
    def average_display(self) -> str:
        if self.average_runtime is None:
            return "-"
        return f"{self.average_runtime:.0f} min"

    @property
    def total_display(self) -> str:
        hours, minutes = divmod(self.total_runtime, 60)
        return f"{hours}h {minutes}m"


@dataclass
class PersonTally:
    person_id: int
    name: str
    profile_path: str | None
    movie_count: int
    # (movie title, year, selector name) for each credit
    entries: list[tuple[str, int | None, str]]


def overview(db: Session) -> dict[str, Any]:
    total_rounds = db.scalar(select(func.count(Round.id))) or 0
    closed_rounds = (
        db.scalar(select(func.count(Round.id)).where(Round.status == RoundStatus.CLOSED))
        or 0
    )
    total_picks = db.scalar(select(func.count(Pick.id))) or 0
    distinct_movies = db.scalar(select(func.count(func.distinct(Pick.movie_id)))) or 0
    total_runtime = (
        db.scalar(
            select(func.sum(Movie.runtime)).join(Pick, Pick.movie_id == Movie.id)
        )
        or 0
    )
    hours, minutes = divmod(int(total_runtime), 60)
    days, hours = divmod(hours, 24)
    return {
        "total_rounds": total_rounds,
        "closed_rounds": closed_rounds,
        "total_picks": total_picks,
        "distinct_movies": distinct_movies,
        "total_runtime_minutes": int(total_runtime),
        "total_runtime_display": f"{days}d {hours}h {minutes}m",
    }


def runtime_by_selector(db: Session) -> list[SelectorRuntime]:
    """Average and total runtime of the films each participant has picked."""
    rows = db.execute(
        select(
            Participant.name,
            func.count(Pick.id),
            func.avg(Movie.runtime),
            func.sum(func.coalesce(Movie.runtime, 0)),
        )
        .join(Pick, Pick.participant_id == Participant.id)
        .join(Movie, Movie.id == Pick.movie_id)
        .group_by(Participant.id)
        .order_by(func.avg(Movie.runtime).desc())
    ).all()
    return [
        SelectorRuntime(
            participant=name,
            movie_count=count,
            average_runtime=float(avg) if avg is not None else None,
            total_runtime=int(total or 0),
        )
        for name, count, avg, total in rows
    ]


def picks_by_participant(db: Session) -> list[dict[str, Any]]:
    """Total movies selected by each person, plus how many rounds they were in."""
    rows = db.execute(
        select(
            Participant.id,
            Participant.name,
            func.count(Pick.id),
            func.min(Round.number),
            func.max(Round.number),
        )
        .join(Pick, Pick.participant_id == Participant.id)
        .join(Round, Round.id == Pick.round_id)
        .group_by(Participant.id)
        .order_by(func.count(Pick.id).desc())
    ).all()
    return [
        {
            "id": pid,
            "name": name,
            "count": count,
            "first_round": first,
            "last_round": last,
        }
        for pid, name, count, first, last in rows
    ]


def _credit_tally(
    db: Session, kind: CreditKind, limit: int, jobs: tuple[str, ...] | None = None
) -> list[PersonTally]:
    """Rank people by how many *distinct* picked films they are credited on."""
    counts = (
        select(
            Credit.person_id.label("person_id"),
            func.count(func.distinct(Pick.movie_id)).label("movie_count"),
        )
        .join(Pick, Pick.movie_id == Credit.movie_id)
        .where(Credit.kind == kind)
        .group_by(Credit.person_id)
        .order_by(func.count(func.distinct(Pick.movie_id)).desc())
        .limit(limit)
    )
    if jobs:
        counts = counts.where(Credit.job.in_(jobs))
    counts = counts.subquery()

    ranked = db.execute(
        select(Person.id, Person.name, Person.profile_path, counts.c.movie_count)
        .join(counts, counts.c.person_id == Person.id)
        .order_by(counts.c.movie_count.desc(), Person.name)
    ).all()
    if not ranked:
        return []

    person_ids = [row[0] for row in ranked]

    detail_q = (
        select(Credit.person_id, Movie.title, Movie.release_date, Participant.name)
        .join(Movie, Movie.id == Credit.movie_id)
        .join(Pick, Pick.movie_id == Movie.id)
        .join(Participant, Participant.id == Pick.participant_id)
        .where(Credit.kind == kind, Credit.person_id.in_(person_ids))
        .order_by(Movie.release_date)
    )
    if jobs:
        detail_q = detail_q.where(Credit.job.in_(jobs))

    details: dict[int, list[tuple[str, int | None, str]]] = {}
    seen: set[tuple[int, str, str]] = set()
    for person_id, title, release_date, selector in db.execute(detail_q).all():
        key = (person_id, title, selector)
        if key in seen:
            continue
        seen.add(key)
        year = release_date.year if release_date else None
        details.setdefault(person_id, []).append((title, year, selector))

    return [
        PersonTally(
            person_id=pid,
            name=name,
            profile_path=profile,
            movie_count=count,
            entries=details.get(pid, []),
        )
        for pid, name, profile, count in ranked
    ]


def top_directors(db: Session, limit: int = 15) -> list[PersonTally]:
    return _credit_tally(db, CreditKind.CREW, limit, jobs=DIRECTOR_JOBS)


def top_actors(db: Session, limit: int = 20) -> list[PersonTally]:
    return _credit_tally(db, CreditKind.CAST, limit)


def top_genres(db: Session, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Genre.name, func.count(func.distinct(Pick.movie_id)))
        .join(MovieGenre, MovieGenre.genre_id == Genre.id)
        .join(Pick, Pick.movie_id == MovieGenre.movie_id)
        .group_by(Genre.id)
        .order_by(func.count(func.distinct(Pick.movie_id)).desc())
        .limit(limit)
    ).all()
    return [{"name": name, "count": count} for name, count in rows]


def picks_by_decade(db: Session) -> list[dict[str, Any]]:
    """Distribution of picked films by release decade."""
    decade = (
        cast(func.substr(func.strftime("%Y", Movie.release_date), 1, 3), Integer) * 10
    )
    rows = db.execute(
        select(decade.label("decade"), func.count(Pick.id))
        .join(Pick, Pick.movie_id == Movie.id)
        .where(Movie.release_date.is_not(None))
        .group_by("decade")
        .order_by("decade")
    ).all()
    return [{"decade": d, "count": c} for d, c in rows if d]
