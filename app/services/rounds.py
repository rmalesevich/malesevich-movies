"""Shared read helpers for assembling a round's view context."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Credit,
    CreditKind,
    Movie,
    Participant,
    Pick,
    Rating,
    Round,
    RoundParticipant,
    RoundStatus,
    Watch,
)


@dataclass
class RoundEntry:
    """One participant's pick within a round, with its directors resolved."""

    participant: Participant
    movie: Movie
    pick: Pick
    directors: list[str]


def open_round(db: Session) -> Round | None:
    return db.scalar(
        select(Round)
        .where(Round.status == RoundStatus.OPEN)
        .order_by(Round.number.desc())
    )


def latest_round(db: Session) -> Round | None:
    return db.scalar(select(Round).order_by(Round.number.desc()))


def next_round_number(db: Session) -> int:
    latest = latest_round(db)
    return (latest.number + 1) if latest else 1


def round_lineup(db: Session, round_: Round) -> list[Participant]:
    return list(
        db.scalars(
            select(Participant)
            .join(RoundParticipant, RoundParticipant.participant_id == Participant.id)
            .where(RoundParticipant.round_id == round_.id)
            .order_by(Participant.joined_round, Participant.name)
        ).all()
    )


def current_lineup(db: Session, round_number: int | None = None) -> list[Participant]:
    """Participants who should be in the round with the given number."""
    people = db.scalars(
        select(Participant).order_by(Participant.joined_round, Participant.name)
    ).all()
    if round_number is None:
        return [p for p in people if p.is_current]
    return [p for p in people if p.is_active_for_round(round_number)]


def directors_by_movie(db: Session, movie_ids: list[int]) -> dict[int, list[str]]:
    if not movie_ids:
        return {}
    rows = db.execute(
        select(Credit.movie_id, Credit.person_id)
        .where(
            Credit.movie_id.in_(movie_ids),
            Credit.kind == CreditKind.CREW,
            Credit.job == "Director",
        )
        .options()
    ).all()
    if not rows:
        return {}
    from app.models import Person

    names = dict(
        db.execute(
            select(Person.id, Person.name).where(
                Person.id.in_({person_id for _, person_id in rows})
            )
        ).all()
    )
    out: dict[int, list[str]] = {}
    for movie_id, person_id in rows:
        name = names.get(person_id)
        if name and name not in out.setdefault(movie_id, []):
            out[movie_id].append(name)
    return out


def round_entries(db: Session, round_: Round) -> list[RoundEntry]:
    picks = db.scalars(
        select(Pick)
        .where(Pick.round_id == round_.id)
        .options(selectinload(Pick.movie), selectinload(Pick.participant))
        .join(Participant, Participant.id == Pick.participant_id)
        .order_by(Participant.joined_round, Participant.name)
    ).all()
    directors = directors_by_movie(db, [p.movie_id for p in picks])
    return [
        RoundEntry(
            participant=pick.participant,
            movie=pick.movie,
            pick=pick,
            directors=directors.get(pick.movie_id, []),
        )
        for pick in picks
    ]


def watch_maps(
    db: Session, round_: Round
) -> tuple[dict[tuple[int, int], Watch], dict[tuple[int, int], Rating]]:
    watches = db.scalars(select(Watch).where(Watch.round_id == round_.id)).all()
    ratings = db.scalars(select(Rating).where(Rating.round_id == round_.id)).all()
    return (
        {(w.participant_id, w.movie_id): w for w in watches},
        {(r.participant_id, r.movie_id): r for r in ratings},
    )


def tagline(db: Session) -> str | None:
    """The masthead tagline, derived from the data.

    Reads "4 people. 4 films. One argument every 22 days." - the headcount is
    the participants still in the lineup, and the interval is the mean gap
    between round start dates, rounded up to whole days.

    Returns ``None`` whenever it cannot be stated honestly: no active
    participants, or fewer than two rounds to measure a gap between. The
    template omits the line entirely in that case rather than printing a
    half-filled sentence.
    """
    people = db.scalar(
        select(func.count(Participant.id)).where(Participant.left_round.is_(None))
    )
    if not people:
        return None

    rounds, first, last = db.execute(
        select(func.count(Round.id), func.min(Round.started_on),
               func.max(Round.started_on))
    ).one()
    if not rounds or rounds < 2 or first is None or last is None:
        return None

    # The mean of the consecutive gaps is just the total span over the number
    # of gaps, so this needs no per-round iteration.
    span_days = (last - first).days
    if span_days <= 0:
        return None
    interval = math.ceil(span_days / (rounds - 1))
    if interval < 1:
        return None

    noun = "person" if people == 1 else "people"
    film = "film" if people == 1 else "films"
    cadence = "every day" if interval == 1 else f"every {interval} days"
    return f"{people} {noun}. {people} {film}. One argument {cadence}."


def runtime_display(minutes: int) -> str:
    hours, mins = divmod(int(minutes or 0), 60)
    return f"{hours}h {mins}m"


def days_since(start: date) -> int:
    return (date.today() - start).days + 1
