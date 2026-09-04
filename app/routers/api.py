"""Small JSON API consumed by the front-end (TMDB autocomplete)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.services.tmdb import TMDBClient, TMDBError, poster_url

router = APIRouter(prefix="/api")


@router.get("/tmdb/search")
def tmdb_search(q: str = Query("", min_length=0), limit: int = 8):
    if not settings.tmdb_enabled:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")
    query = q.strip()
    if len(query) < 2:
        return {"results": []}

    try:
        results = TMDBClient().search_movies(query)
    except TMDBError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = []
    for item in results[:limit]:
        release = item.get("release_date") or ""
        payload.append(
            {
                "id": item["id"],
                "title": item.get("title") or item.get("original_title") or "Untitled",
                "year": release[:4] or None,
                "overview": item.get("overview") or "",
                "poster_url": poster_url(item.get("poster_path"), "w92"),
            }
        )
    return {"results": payload}
