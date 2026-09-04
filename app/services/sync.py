"""Reconcile Trakt watch/rating history against the movies in a round."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Movie,
    Participant,
    Pick,
    Rating,
    RecordSource,
    Round,
    RoundParticipant,
    RoundStatus,
    SyncLog,
    Watch,
    utcnow,
)
from app.services.trakt import TraktClient, TraktError, parse_trakt_datetime

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    watches_added: int = 0
    watches_updated: int = 0
    ratings_added: int = 0
    ratings_updated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.watches_added} new watches",
            f"{self.watches_updated} updated",
            f"{self.ratings_added} new ratings",
            f"{self.ratings_updated} updated",
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} error(s): " + "; ".join(self.errors))
        return ", ".join(parts)


def _naive_utc(value: datetime | None) -> datetime | None:
    """SQLite columns are naive, so normalise everything to naive UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def sync_round(
    db: Session, round_: Round, client: TraktClient | None = None
) -> SyncResult:
    """Pull Trakt history and ratings for every participant in ``round_``.

    Only the films picked in that round are considered, but the two record
    types are scoped differently:

    * **Watches** are bounded by the round's window (start date through the
      discussion date, or today while the round is open). "Did you watch it
      for this round?" is inherently a question about that window.
    * **Ratings** are *not* windowed. A rating is a standing opinion about a
      film, not an event: someone who saw a film years ago and rated it then
      will never re-rate it just because it came up in a round, so filtering
      by date would silently drop their score.
    """
    result = SyncResult()
    client = client or TraktClient()

    movies = db.scalars(
        select(Movie).join(Pick, Pick.movie_id == Movie.id).where(
            Pick.round_id == round_.id
        )
    ).all()
    movies_by_tmdb: dict[int, Movie] = {m.tmdb_id: m for m in movies}

    if not movies_by_tmdb:
        result.errors.append(f"Round {round_.number} has no movies to check")
        return result

    lineup = db.scalars(
        select(Participant)
        .join(RoundParticipant, RoundParticipant.participant_id == Participant.id)
        .where(RoundParticipant.round_id == round_.id)
        .order_by(Participant.name)
    ).all()

    window_start = round_.started_on
    window_end = round_.end_date

    for participant in lineup:
        if not participant.trakt_username:
            continue
        try:
            history = client.movie_history(
                participant.trakt_username, window_start, window_end
            )
        except TraktError as exc:
            msg = f"{participant.name}: history - {exc}"
            log.warning(msg)
            result.errors.append(msg)
            history = []

        # Trakt returns one row per play, so collapse them per film first:
        # the round cares about the *first* viewing, plus how many replays.
        plays_by_movie: dict[int, list[datetime]] = {}
        for entry in history:
            payload = entry.get("movie") or {}
            ids = payload.get("ids") or {}
            movie = movies_by_tmdb.get(ids.get("tmdb"))
            if movie is None:
                continue
            # Opportunistically record the Trakt ids on the movie.
            if ids.get("trakt") and not movie.trakt_id:
                movie.trakt_id = ids["trakt"]
                movie.trakt_slug = ids.get("slug")

            watched_at = _naive_utc(parse_trakt_datetime(entry.get("watched_at")))
            if watched_at is not None:
                plays_by_movie.setdefault(movie.id, []).append(watched_at)
            else:
                plays_by_movie.setdefault(movie.id, [])

        for movie_id, timestamps in plays_by_movie.items():
            first_watch = min(timestamps) if timestamps else None
            play_count = max(len(timestamps), 1)

            existing = db.scalar(
                select(Watch).where(
                    Watch.round_id == round_.id,
                    Watch.participant_id == participant.id,
                    Watch.movie_id == movie_id,
                )
            )
            if existing is None:
                db.add(
                    Watch(
                        round_id=round_.id,
                        participant_id=participant.id,
                        movie_id=movie_id,
                        watched_at=first_watch,
                        plays=play_count,
                        source=RecordSource.TRAKT,
                    )
                )
                result.watches_added += 1
            elif existing.source == RecordSource.TRAKT:
                # Assign rather than increment, so a re-sync is idempotent.
                changed = False
                if first_watch and existing.watched_at != first_watch:
                    existing.watched_at = first_watch
                    changed = True
                if existing.plays != play_count:
                    existing.plays = play_count
                    changed = True
                if changed:
                    result.watches_updated += 1

        try:
            ratings = client.movie_ratings(participant.trakt_username)
        except TraktError as exc:
            msg = f"{participant.name}: ratings - {exc}"
            log.warning(msg)
            result.errors.append(msg)
            ratings = []

        for entry in ratings:
            payload = entry.get("movie") or {}
            ids = payload.get("ids") or {}
            movie = movies_by_tmdb.get(ids.get("tmdb"))
            if movie is None:
                continue
            # Deliberately not filtered by the round window - see the
            # docstring. rated_at is still stored so the UI can tell a fresh
            # score from one carried over from an earlier viewing.
            rated_at = _naive_utc(parse_trakt_datetime(entry.get("rated_at")))
            score = entry.get("rating")
            if not score:
                continue

            existing = db.scalar(
                select(Rating).where(
                    Rating.round_id == round_.id,
                    Rating.participant_id == participant.id,
                    Rating.movie_id == movie.id,
                )
            )
            if existing is None:
                db.add(
                    Rating(
                        round_id=round_.id,
                        participant_id=participant.id,
                        movie_id=movie.id,
                        rating=score,
                        rated_at=rated_at,
                        source=RecordSource.TRAKT,
                    )
                )
                result.ratings_added += 1
            elif existing.source == RecordSource.TRAKT and (
                existing.rating != score or existing.rated_at != rated_at
            ):
                existing.rating = score
                existing.rated_at = rated_at
                result.ratings_updated += 1

    db.flush()
    return result


def sync_open_rounds(db: Session, client: TraktClient | None = None) -> SyncResult:
    """The job the daily scheduler runs: refresh every round still open."""
    combined = SyncResult()
    rounds = db.scalars(
        select(Round).where(Round.status == RoundStatus.OPEN).order_by(Round.number)
    ).all()

    if not rounds:
        log.info("No open rounds to sync")
        return combined

    client = client or TraktClient()
    for round_ in rounds:
        entry = SyncLog(kind="trakt", round_number=round_.number, started_at=utcnow())
        db.add(entry)
        try:
            result = sync_round(db, round_, client)
        except Exception as exc:
            log.exception("Sync failed for round %s", round_.number)
            entry.ok = False
            entry.message = str(exc)[:1000]
            entry.finished_at = utcnow()
            combined.errors.append(f"round {round_.number}: {exc}")
            db.commit()
            continue

        entry.ok = result.ok
        entry.watches_added = result.watches_added
        entry.ratings_added = result.ratings_added
        entry.message = result.summary()[:1000]
        entry.finished_at = utcnow()

        combined.watches_added += result.watches_added
        combined.watches_updated += result.watches_updated
        combined.ratings_added += result.ratings_added
        combined.ratings_updated += result.ratings_updated
        combined.errors.extend(result.errors)
        db.commit()

    return combined
