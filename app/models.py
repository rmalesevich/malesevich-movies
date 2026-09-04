"""SQLAlchemy models for the Malesevich Movies project."""
from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class RoundStatus(enum.StrEnum):
    OPEN = "open"        # movies picked, currently being watched
    CLOSED = "closed"    # discussed and archived


class CreditKind(enum.StrEnum):
    CAST = "cast"
    CREW = "crew"


class RecordSource(enum.StrEnum):
    TRAKT = "trakt"
    MANUAL = "manual"


# --------------------------------------------------------------------------
# People taking part
# --------------------------------------------------------------------------
class Participant(Base):
    """A family member who picks movies."""

    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    trakt_username: Mapped[str | None] = mapped_column(String(120))
    # Round numbers bounding participation. joined_round is inclusive,
    # left_round is inclusive of the last round they took part in.
    joined_round: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    left_round: Mapped[int | None] = mapped_column(Integer)
    color: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    picks: Mapped[list[Pick]] = relationship(back_populates="participant")
    memberships: Mapped[list[RoundParticipant]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )

    def is_active_for_round(self, number: int) -> bool:
        if number < self.joined_round:
            return False
        return self.left_round is None or number <= self.left_round

    @property
    def is_current(self) -> bool:
        return self.left_round is None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Participant {self.name}>"


# --------------------------------------------------------------------------
# Rounds
# --------------------------------------------------------------------------
class Round(Base):
    """One cycle: everybody picks a film, we watch them, then we discuss."""

    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    status: Mapped[RoundStatus] = mapped_column(
        Enum(RoundStatus, native_enum=False), default=RoundStatus.OPEN, nullable=False
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The date of the discussion, which also closes the round.
    discussed_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    picks: Mapped[list[Pick]] = relationship(
        back_populates="round", cascade="all, delete-orphan", order_by="Pick.id"
    )
    participants: Mapped[list[RoundParticipant]] = relationship(
        back_populates="round", cascade="all, delete-orphan"
    )
    watches: Mapped[list[Watch]] = relationship(
        back_populates="round", cascade="all, delete-orphan"
    )
    ratings: Mapped[list[Rating]] = relationship(
        back_populates="round", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.status == RoundStatus.OPEN

    @property
    def end_date(self) -> date:
        """Upper bound used when querying Trakt history for this round."""
        return self.discussed_on or date.today()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Round {self.number} {self.status.value}>"


class RoundParticipant(Base):
    """Snapshot of who was in the lineup for a given round."""

    __tablename__ = "round_participants"
    __table_args__ = (UniqueConstraint("round_id", "participant_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )

    round: Mapped[Round] = relationship(back_populates="participants")
    participant: Mapped[Participant] = relationship(back_populates="memberships")


class Pick(Base):
    """The film one participant selected for one round."""

    __tablename__ = "picks"
    __table_args__ = (UniqueConstraint("round_id", "participant_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    round: Mapped[Round] = relationship(back_populates="picks")
    participant: Mapped[Participant] = relationship(back_populates="picks")
    movie: Mapped[Movie] = relationship(back_populates="picks")


# --------------------------------------------------------------------------
# Films and the people who made them
# --------------------------------------------------------------------------
class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(20), index=True)
    trakt_id: Mapped[int | None] = mapped_column(Integer, index=True)
    trakt_slug: Mapped[str | None] = mapped_column(String(200))

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    original_title: Mapped[str | None] = mapped_column(String(300))
    tagline: Mapped[str | None] = mapped_column(Text)
    overview: Mapped[str | None] = mapped_column(Text)
    release_date: Mapped[date | None] = mapped_column(Date)
    runtime: Mapped[int | None] = mapped_column(Integer)  # minutes
    poster_path: Mapped[str | None] = mapped_column(String(200))
    backdrop_path: Mapped[str | None] = mapped_column(String(200))
    original_language: Mapped[str | None] = mapped_column(String(10))
    tmdb_vote_average: Mapped[float | None] = mapped_column(Float)
    metadata_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    picks: Mapped[list[Pick]] = relationship(back_populates="movie")
    credits: Mapped[list[Credit]] = relationship(
        back_populates="movie", cascade="all, delete-orphan"
    )
    genres: Mapped[list[MovieGenre]] = relationship(
        back_populates="movie", cascade="all, delete-orphan"
    )

    @property
    def year(self) -> int | None:
        return self.release_date.year if self.release_date else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Movie {self.title} ({self.year})>"


class Person(Base):
    """A cast or crew member, deduplicated by TMDB person id."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_path: Mapped[str | None] = mapped_column(String(200))
    known_for_department: Mapped[str | None] = mapped_column(String(80))

    credits: Mapped[list[Credit]] = relationship(back_populates="person")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Person {self.name}>"


class Credit(Base):
    """Links a person to a movie, either as cast or as crew."""

    __tablename__ = "credits"
    __table_args__ = (
        UniqueConstraint("movie_id", "person_id", "kind", "job", "character"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[CreditKind] = mapped_column(
        Enum(CreditKind, native_enum=False), nullable=False, index=True
    )
    department: Mapped[str | None] = mapped_column(String(80), index=True)
    job: Mapped[str | None] = mapped_column(String(120), index=True)
    character: Mapped[str | None] = mapped_column(String(300))
    billing_order: Mapped[int | None] = mapped_column(Integer)

    movie: Mapped[Movie] = relationship(back_populates="credits")
    person: Mapped[Person] = relationship(back_populates="credits")


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)

    movies: Mapped[list[MovieGenre]] = relationship(back_populates="genre")


class MovieGenre(Base):
    __tablename__ = "movie_genres"
    __table_args__ = (UniqueConstraint("movie_id", "genre_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=False
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genres.id", ondelete="CASCADE"), nullable=False
    )

    movie: Mapped[Movie] = relationship(back_populates="genres")
    genre: Mapped[Genre] = relationship(back_populates="movies")


# --------------------------------------------------------------------------
# What everybody actually watched / thought
# --------------------------------------------------------------------------
class Watch(Base):
    """One participant watched one movie during one round."""

    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("round_id", "participant_id", "movie_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    watched_at: Mapped[datetime | None] = mapped_column(DateTime)
    plays: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[RecordSource] = mapped_column(
        Enum(RecordSource, native_enum=False), default=RecordSource.TRAKT, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    round: Mapped[Round] = relationship(back_populates="watches")
    participant: Mapped[Participant] = relationship()
    movie: Mapped[Movie] = relationship()


class Rating(Base):
    """One participant's Trakt rating (1-10) for a movie in a round."""

    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("round_id", "participant_id", "movie_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[RecordSource] = mapped_column(
        Enum(RecordSource, native_enum=False), default=RecordSource.TRAKT, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    round: Mapped[Round] = relationship(back_populates="ratings")
    participant: Mapped[Participant] = relationship()
    movie: Mapped[Movie] = relationship()


class SyncLog(Base):
    """Audit trail for the daily Trakt sync so failures are visible in the UI."""

    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(40), default="trakt")
    round_number: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    watches_added: Mapped[int] = mapped_column(Integer, default=0)
    ratings_added: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
