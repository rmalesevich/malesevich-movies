"""TheMovieDB client and the mapping from TMDB payloads into our models."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Credit, CreditKind, Genre, Movie, MovieGenre, Person

log = logging.getLogger(__name__)

# How many billed cast members to keep per film. The long tail of uncredited
# extras only adds noise to the "most covered actors" statistic.
CAST_LIMIT = 20

# Crew jobs worth storing. Everything else is dropped to keep the table small.
CREW_JOBS = {
    "Director",
    "Writer",
    "Screenplay",
    "Story",
    "Producer",
    "Executive Producer",
    "Director of Photography",
    "Original Music Composer",
    "Editor",
}


class TMDBError(RuntimeError):
    pass


class TMDBClient:
    """Thin wrapper over the TMDB v3 REST API.

    Accepts either a v3 API key or a v4 read access token; the latter is a JWT
    and has to go in the Authorization header instead of the query string.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.tmdb_api_key
        self.base_url = (base_url or settings.tmdb_base_url).rstrip("/")
        self._is_bearer = self.api_key.startswith("eyJ")

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self._is_bearer:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"language": settings.tmdb_language}
        if not self._is_bearer:
            params["api_key"] = self.api_key
        params.update(extra or {})
        return params

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise TMDBError("TMDB_API_KEY is not configured")
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.get(
                url, params=self._params(params), headers=self._headers(), timeout=20.0
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TMDBError(
                f"TMDB {exc.response.status_code} for {path}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TMDBError(f"TMDB request failed for {path}: {exc}") from exc
        return resp.json()

    # -- endpoints ---------------------------------------------------------
    def search_movies(self, query: str, page: int = 1) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        data = self._get(
            "/search/movie",
            {"query": query, "page": page, "include_adult": "false"},
        )
        return data.get("results", [])

    def movie(self, tmdb_id: int) -> dict[str, Any]:
        return self._get(
            f"/movie/{tmdb_id}",
            {"append_to_response": "credits,external_ids"},
        )


# --------------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def poster_url(path: str | None, size: str = "w342") -> str | None:
    if not path:
        return None
    return f"{settings.tmdb_image_base}/{size}{path}"


def _upsert_person(db: Session, payload: dict[str, Any]) -> Person | None:
    tmdb_id = payload.get("id")
    name = payload.get("name")
    if not tmdb_id or not name:
        return None
    person = db.scalar(select(Person).where(Person.tmdb_id == tmdb_id))
    if person is None:
        person = Person(tmdb_id=tmdb_id, name=name)
        db.add(person)
    person.name = name
    person.profile_path = payload.get("profile_path")
    person.known_for_department = payload.get("known_for_department")
    return person


def _sync_genres(db: Session, movie: Movie, payload: dict[str, Any]) -> None:
    movie.genres.clear()
    db.flush()
    for item in payload.get("genres", []) or []:
        genre = db.scalar(select(Genre).where(Genre.tmdb_id == item["id"]))
        if genre is None:
            genre = Genre(tmdb_id=item["id"], name=item["name"])
            db.add(genre)
            db.flush()
        db.add(MovieGenre(movie_id=movie.id, genre_id=genre.id))


def _sync_credits(db: Session, movie: Movie, payload: dict[str, Any]) -> None:
    credits = payload.get("credits") or {}

    # Replace wholesale: TMDB credits get corrected over time and the churn is
    # small enough that a diff is not worth the complexity.
    for existing in list(movie.credits):
        db.delete(existing)
    db.flush()

    seen: set[tuple[int, str, str | None, str | None]] = set()

    for entry in (credits.get("cast") or [])[:CAST_LIMIT]:
        person = _upsert_person(db, entry)
        if person is None:
            continue
        db.flush()
        character = entry.get("character")
        key = (person.id, CreditKind.CAST.value, None, character)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            Credit(
                movie_id=movie.id,
                person_id=person.id,
                kind=CreditKind.CAST,
                department="Acting",
                character=character,
                billing_order=entry.get("order"),
            )
        )

    for entry in credits.get("crew") or []:
        if entry.get("job") not in CREW_JOBS:
            continue
        person = _upsert_person(db, entry)
        if person is None:
            continue
        db.flush()
        job = entry.get("job")
        key = (person.id, CreditKind.CREW.value, job, None)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            Credit(
                movie_id=movie.id,
                person_id=person.id,
                kind=CreditKind.CREW,
                department=entry.get("department"),
                job=job,
            )
        )


def upsert_movie_from_tmdb(
    db: Session, tmdb_id: int, client: TMDBClient | None = None, refresh: bool = False
) -> Movie:
    """Fetch a film from TMDB and store it with its credits and genres.

    If the movie is already present and has been synced, it is returned as-is
    unless ``refresh`` is set.
    """
    movie = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
    if movie is not None and movie.metadata_synced_at and not refresh:
        return movie

    client = client or TMDBClient()
    payload = client.movie(tmdb_id)

    if movie is None:
        movie = Movie(tmdb_id=tmdb_id, title=payload.get("title") or "Untitled")
        db.add(movie)
        db.flush()

    movie.title = payload.get("title") or movie.title
    movie.original_title = payload.get("original_title")
    movie.tagline = payload.get("tagline") or None
    movie.overview = payload.get("overview")
    movie.release_date = _parse_date(payload.get("release_date"))
    movie.runtime = payload.get("runtime") or None
    movie.poster_path = payload.get("poster_path")
    movie.backdrop_path = payload.get("backdrop_path")
    movie.original_language = payload.get("original_language")
    movie.tmdb_vote_average = payload.get("vote_average")
    movie.imdb_id = (payload.get("external_ids") or {}).get("imdb_id") or payload.get(
        "imdb_id"
    )
    movie.metadata_synced_at = datetime.now(UTC)

    _sync_genres(db, movie, payload)
    _sync_credits(db, movie, payload)
    db.flush()
    log.info("Synced TMDB metadata for %s (%s)", movie.title, tmdb_id)
    return movie
