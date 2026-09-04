"""Helpers that build a small but realistic slice of the domain."""
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import (
    Credit,
    CreditKind,
    Movie,
    Participant,
    Person,
    Pick,
    Rating,
    Round,
    RoundParticipant,
    RoundStatus,
    Watch,
)

_counter = {"tmdb": 1000}


def next_tmdb_id() -> int:
    _counter["tmdb"] += 1
    return _counter["tmdb"]


def make_participant(db: Session, name: str, joined_round: int = 1, **kwargs):
    person = Participant(name=name, joined_round=joined_round, **kwargs)
    db.add(person)
    db.flush()
    return person


def make_movie(db: Session, title: str, runtime: int = 120, year: int = 2000, **kwargs):
    movie = Movie(
        tmdb_id=kwargs.pop("tmdb_id", next_tmdb_id()),
        title=title,
        runtime=runtime,
        release_date=date(year, 6, 1),
        overview=f"{title} is a film.",
        metadata_synced_at=datetime(2024, 1, 1),
        **kwargs,
    )
    db.add(movie)
    db.flush()
    return movie


def credit(db: Session, movie: Movie, name: str, kind: CreditKind, job=None, order=None):
    person = db.query(Person).filter_by(name=name).one_or_none()
    if person is None:
        person = Person(tmdb_id=next_tmdb_id(), name=name)
        db.add(person)
        db.flush()
    db.add(
        Credit(
            movie_id=movie.id,
            person_id=person.id,
            kind=kind,
            job=job,
            department="Directing" if job == "Director" else "Acting",
            billing_order=order,
        )
    )
    db.flush()
    return person


def make_round(db: Session, number: int, participants, status=RoundStatus.OPEN, **kwargs):
    round_ = Round(
        number=number,
        started_on=kwargs.pop("started_on", date(2024, 1, 1)),
        status=status,
        **kwargs,
    )
    db.add(round_)
    db.flush()
    for participant in participants:
        db.add(RoundParticipant(round_id=round_.id, participant_id=participant.id))
    db.flush()
    return round_


def make_pick(db: Session, round_: Round, participant: Participant, movie: Movie):
    pick = Pick(round_id=round_.id, participant_id=participant.id, movie_id=movie.id)
    db.add(pick)
    db.flush()
    return pick


def make_watch(db: Session, round_, participant, movie, when=datetime(2024, 1, 15)):
    watch = Watch(
        round_id=round_.id,
        participant_id=participant.id,
        movie_id=movie.id,
        watched_at=when,
    )
    db.add(watch)
    db.flush()
    return watch


def make_rating(db: Session, round_, participant, movie, score=8):
    rating = Rating(
        round_id=round_.id,
        participant_id=participant.id,
        movie_id=movie.id,
        rating=score,
        rated_at=datetime(2024, 1, 16),
    )
    db.add(rating)
    db.flush()
    return rating
