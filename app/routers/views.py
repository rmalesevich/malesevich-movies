"""Read-only views: current round, round archive, statistics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Movie, Pick, Round, SyncLog
from app.services import rounds as round_service
from app.services import stats as stats_service
from app.templating import render

router = APIRouter()


@router.get("/")
def current_round(request: Request, db: Session = Depends(get_db)):
    round_ = round_service.open_round(db) or round_service.latest_round(db)
    if round_ is None:
        return render(request, "index.html", {"nav": "current", "round": None})

    entries = round_service.round_entries(db, round_)
    lineup = round_service.round_lineup(db, round_)
    watch_map, rating_map = round_service.watch_maps(db, round_)

    total_runtime = sum(e.movie.runtime or 0 for e in entries)
    last_sync = db.scalar(select(SyncLog).order_by(SyncLog.started_at.desc()))

    return render(
        request,
        "index.html",
        {
            "nav": "current",
            "round": round_,
            "entries": entries,
            "lineup": lineup,
            "watch_map": watch_map,
            "rating_map": rating_map,
            "days_elapsed": round_service.days_since(round_.started_on),
            "has_carried_ratings": any(
                r.rated_at and r.rated_at.date() < round_.started_on
                for r in rating_map.values()
            ),
            "watched_count": len(watch_map),
            "possible_count": len(entries) * len(lineup),
            "total_runtime_display": round_service.runtime_display(total_runtime),
            "last_sync_display": (
                last_sync.started_at.strftime("%b %d") if last_sync else "never"
            ),
        },
    )


@router.get("/rounds")
def round_list(request: Request, db: Session = Depends(get_db)):
    all_rounds = db.scalars(select(Round).order_by(Round.number.desc())).all()
    counts = dict(
        db.execute(
            select(Pick.round_id, func.count(Pick.id)).group_by(Pick.round_id)
        ).all()
    )
    titles: dict[int, list[str]] = {}
    for round_id, title in db.execute(
        select(Pick.round_id, Movie.title).join(Movie, Movie.id == Pick.movie_id)
    ).all():
        titles.setdefault(round_id, []).append(title)

    rows = [
        {
            "round": r,
            "pick_count": counts.get(r.id, 0),
            "titles": ", ".join(titles.get(r.id, [])),
        }
        for r in all_rounds
    ]
    first_date = min((r.started_on for r in all_rounds), default=None)
    return render(
        request,
        "rounds.html",
        {"nav": "rounds", "rows": rows, "first_date": first_date},
    )


@router.get("/rounds/{number}")
def round_detail(number: int, request: Request, db: Session = Depends(get_db)):
    round_ = db.scalar(select(Round).where(Round.number == number))
    if round_ is None:
        raise HTTPException(status_code=404, detail=f"Round {number} not found")

    entries = round_service.round_entries(db, round_)
    lineup = round_service.round_lineup(db, round_)
    watch_map, rating_map = round_service.watch_maps(db, round_)

    prev_round = db.scalar(
        select(Round.number).where(Round.number < number).order_by(Round.number.desc())
    )
    next_round = db.scalar(
        select(Round.number).where(Round.number > number).order_by(Round.number)
    )

    return render(
        request,
        "round_detail.html",
        {
            "nav": "rounds",
            "round": round_,
            "entries": entries,
            "lineup": lineup,
            "watch_map": watch_map,
            "rating_map": rating_map,
            "prev_round": prev_round,
            "next_round": next_round,
        },
    )


@router.get("/stats")
def statistics(request: Request, db: Session = Depends(get_db)):
    runtimes = stats_service.runtime_by_selector(db)
    genres = stats_service.top_genres(db)
    decades = stats_service.picks_by_decade(db)
    return render(
        request,
        "stats.html",
        {
            "nav": "stats",
            "overview": stats_service.overview(db),
            "runtimes": runtimes,
            "max_avg_runtime": max(
                (r.average_runtime for r in runtimes if r.average_runtime), default=0
            ),
            "picks": stats_service.picks_by_participant(db),
            "directors": stats_service.top_directors(db),
            "actors": stats_service.top_actors(db),
            "genres": genres,
            "decades": decades,
            "max_decade_count": max((d["count"] for d in decades), default=1),
        },
    )
