"""The Physical Media shelf: which discussed films are on the shelf already."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MediaFormat, Movie
from app.services import physical as shelf_service
from app.templating import flash, render

router = APIRouter(prefix="/physical-media")

# Which slice of the shelf to show. Anything else falls back to everything.
FILTERS = ("all", "owned", "missing")


def _redirect(url: str) -> RedirectResponse:
    # 303 so the browser turns the POST into a GET.
    return RedirectResponse(url, status_code=303)


@router.get("")
def shelf_page(request: Request, show: str = "all", db: Session = Depends(get_db)):
    show = show if show in FILTERS else "all"
    entries = shelf_service.shelf(db)
    overview = shelf_service.overview(db, entries)

    if show == "owned":
        visible = [e for e in entries if e.owned]
    elif show == "missing":
        visible = [e for e in entries if not e.owned]
    else:
        visible = entries

    return render(
        request,
        "physical_media.html",
        {
            "nav": "physical",
            "overview": overview,
            "entries": visible,
            "formats": shelf_service.FORMATS,
            "show": show,
        },
    )


@router.post("/{movie_id}/toggle")
def toggle(
    movie_id: int,
    request: Request,
    format: str = Form(...),
    show: str = Form("all"),
    db: Session = Depends(get_db),
):
    """Flip one format on one film, then land back on the same card."""
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    try:
        fmt = MediaFormat(format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown format {format!r}") from exc

    added = shelf_service.toggle_format(db, movie, fmt)
    db.commit()
    flash(
        request,
        f"{'Added' if added else 'Removed'} {fmt.label} for {movie.title}.",
    )

    show = show if show in FILTERS else "all"
    query = "" if show == "all" else f"?show={show}"
    return _redirect(f"/physical-media{query}#movie-{movie_id}")
