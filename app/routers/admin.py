"""Management interface: participants, rounds, picks."""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import (
    Participant,
    Pick,
    Round,
    RoundParticipant,
    RoundStatus,
    SyncLog,
)
from app.services import rounds as round_service
from app.services.participants import clean_name, find_by_name
from app.services.sync import sync_round
from app.services.tmdb import TMDBClient, TMDBError, upsert_movie_from_tmdb
from app.services.trakt import TraktClient
from app.templating import flash, render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _redirect(url: str) -> RedirectResponse:
    # 303 so the browser turns the POST into a GET.
    return RedirectResponse(url, status_code=303)


def _optional_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def _optional_date(value: str | None) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _get_round(db: Session, round_id: int) -> Round:
    round_ = db.get(Round, round_id)
    if round_ is None:
        raise HTTPException(status_code=404, detail="Round not found")
    return round_


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------
@router.get("/participants")
def participants_page(request: Request, db: Session = Depends(get_db)):
    people = db.scalars(
        select(Participant).order_by(Participant.joined_round, Participant.name)
    ).all()
    counts = dict(
        db.execute(
            select(Pick.participant_id, func.count(Pick.id)).group_by(
                Pick.participant_id
            )
        ).all()
    )
    rows = [
        {"participant": p, "pick_count": counts.get(p.id, 0)} for p in people
    ]
    return render(
        request,
        "admin/participants.html",
        {
            "nav": "admin-participants",
            "rows": rows,
            "next_round_number": round_service.next_round_number(db),
        },
    )


@router.post("/participants")
def create_participant(
    request: Request,
    name: str = Form(...),
    trakt_username: str = Form(""),
    joined_round: str = Form("1"),
    db: Session = Depends(get_db),
):
    name = clean_name(name)
    existing = find_by_name(db, name)
    if existing:
        flash(
            request,
            f"{existing.name} already exists - names are matched ignoring case "
            "and spacing.",
            "error",
        )
        return _redirect("/admin/participants")

    db.add(
        Participant(
            name=name,
            trakt_username=(trakt_username or "").strip() or None,
            joined_round=_optional_int(joined_round) or 1,
        )
    )
    db.commit()
    flash(request, f"Added {name}.")
    return _redirect("/admin/participants")


@router.post("/participants/{participant_id}")
def update_participant(
    participant_id: int,
    request: Request,
    name: str = Form(...),
    trakt_username: str = Form(""),
    joined_round: str = Form("1"),
    left_round: str = Form(""),
    db: Session = Depends(get_db),
):
    participant = db.get(Participant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found")

    name = clean_name(name)
    clash = find_by_name(db, name)
    if clash and clash.id != participant.id:
        flash(request, f"{clash.name} already uses that name.", "error")
        return _redirect("/admin/participants")

    participant.name = name
    participant.trakt_username = (trakt_username or "").strip() or None
    participant.joined_round = _optional_int(joined_round) or 1
    participant.left_round = _optional_int(left_round)
    db.commit()
    flash(request, f"Saved {participant.name}.")
    return _redirect("/admin/participants")


@router.post("/participants/{participant_id}/delete")
def delete_participant(
    participant_id: int, request: Request, db: Session = Depends(get_db)
):
    participant = db.get(Participant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    name = participant.name
    db.delete(participant)
    db.commit()
    flash(request, f"Deleted {name}.")
    return _redirect("/admin/participants")


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------
@router.get("/rounds")
def rounds_page(request: Request, db: Session = Depends(get_db)):
    all_rounds = db.scalars(select(Round).order_by(Round.number.desc())).all()
    counts = dict(
        db.execute(
            select(Pick.round_id, func.count(Pick.id)).group_by(Pick.round_id)
        ).all()
    )
    open_round = round_service.open_round(db)
    next_number = round_service.next_round_number(db)

    return render(
        request,
        "admin/rounds.html",
        {
            "nav": "admin-rounds",
            "rows": [
                {"round": r, "pick_count": counts.get(r.id, 0)} for r in all_rounds
            ],
            "open_round": open_round,
            "open_pick_count": counts.get(open_round.id, 0) if open_round else 0,
            "open_lineup_count": (
                len(round_service.round_lineup(db, open_round)) if open_round else 0
            ),
            "next_round_number": next_number,
            "current_lineup": round_service.current_lineup(db, next_number),
            "today": date.today().isoformat(),
            "sync_logs": db.scalars(
                select(SyncLog).order_by(SyncLog.started_at.desc()).limit(10)
            ).all(),
        },
    )


def _create_round(db: Session, number: int, started_on: date) -> Round:
    round_ = Round(number=number, started_on=started_on, status=RoundStatus.OPEN)
    db.add(round_)
    db.flush()
    for participant in round_service.current_lineup(db, number):
        db.add(RoundParticipant(round_id=round_.id, participant_id=participant.id))
    return round_


@router.post("/rounds")
def create_round(
    request: Request,
    started_on: str = Form(...),
    number: str = Form(...),
    db: Session = Depends(get_db),
):
    round_number = _optional_int(number) or round_service.next_round_number(db)
    if db.scalar(select(Round).where(Round.number == round_number)):
        flash(request, f"Round {round_number} already exists.", "error")
        return _redirect("/admin/rounds")

    round_ = _create_round(db, round_number, date.fromisoformat(started_on))
    db.commit()
    flash(request, f"Round {round_number} is open. Now pick the films.")
    return _redirect(f"/admin/rounds/{round_.id}")


@router.post("/rounds/{round_id}/close")
def close_round(
    round_id: int,
    request: Request,
    discussed_on: str = Form(...),
    next_started_on: str = Form(...),
    open_next: str = Form(""),
    db: Session = Depends(get_db),
):
    round_ = _get_round(db, round_id)
    round_.status = RoundStatus.CLOSED
    round_.discussed_on = date.fromisoformat(discussed_on)

    message = f"Closed round {round_.number}."
    target = "/admin/rounds"
    if open_next:
        next_number = round_.number + 1
        if db.scalar(select(Round).where(Round.number == next_number)):
            message += f" Round {next_number} already existed, so it was left alone."
        else:
            new_round = _create_round(
                db, next_number, date.fromisoformat(next_started_on)
            )
            db.flush()
            message += f" Round {next_number} is now open."
            target = f"/admin/rounds/{new_round.id}"

    db.commit()
    flash(request, message)
    return _redirect(target)


@router.get("/rounds/{round_id}")
def edit_round(round_id: int, request: Request, db: Session = Depends(get_db)):
    round_ = _get_round(db, round_id)
    lineup = round_service.round_lineup(db, round_)
    picks = {
        pick.participant_id: pick
        for pick in db.scalars(select(Pick).where(Pick.round_id == round_.id)).all()
    }
    movie_ids = [pick.movie_id for pick in picks.values()]
    directors = round_service.directors_by_movie(db, movie_ids)

    slots = []
    for participant in lineup:
        pick = picks.get(participant.id)
        movie = pick.movie if pick else None
        slots.append(
            {
                "participant": participant,
                "pick": pick,
                "movie": movie,
                "directors": directors.get(movie.id, []) if movie else [],
            }
        )

    in_round = {p.id for p in lineup}
    available = [
        p
        for p in db.scalars(select(Participant).order_by(Participant.name)).all()
        if p.id not in in_round
    ]

    return render(
        request,
        "admin/round_edit.html",
        {
            "nav": "admin-rounds",
            "round": round_,
            "slots": slots,
            "available": available,
        },
    )


@router.post("/rounds/{round_id}")
def update_round(
    round_id: int,
    request: Request,
    started_on: str = Form(...),
    discussed_on: str = Form(""),
    status: str = Form("open"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    round_ = _get_round(db, round_id)
    round_.started_on = date.fromisoformat(started_on)
    round_.discussed_on = _optional_date(discussed_on)
    round_.status = RoundStatus(status)
    round_.notes = (notes or "").strip() or None
    db.commit()
    flash(request, f"Saved round {round_.number}.")
    return _redirect(f"/admin/rounds/{round_id}")


@router.post("/rounds/{round_id}/delete")
def delete_round(round_id: int, request: Request, db: Session = Depends(get_db)):
    round_ = _get_round(db, round_id)
    number = round_.number
    db.delete(round_)
    db.commit()
    flash(request, f"Deleted round {number}.")
    return _redirect("/admin/rounds")


# ---------------------------------------------------------------------------
# Lineup within a round
# ---------------------------------------------------------------------------
@router.post("/rounds/{round_id}/lineup")
def add_to_lineup(
    round_id: int,
    request: Request,
    participant_id: int = Form(...),
    db: Session = Depends(get_db),
):
    round_ = _get_round(db, round_id)
    exists = db.scalar(
        select(RoundParticipant).where(
            RoundParticipant.round_id == round_.id,
            RoundParticipant.participant_id == participant_id,
        )
    )
    if not exists:
        db.add(
            RoundParticipant(round_id=round_.id, participant_id=participant_id)
        )
        db.commit()
    return _redirect(f"/admin/rounds/{round_id}")


@router.post("/rounds/{round_id}/lineup/{participant_id}/remove")
def remove_from_lineup(
    round_id: int,
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    membership = db.scalar(
        select(RoundParticipant).where(
            RoundParticipant.round_id == round_id,
            RoundParticipant.participant_id == participant_id,
        )
    )
    if membership:
        db.delete(membership)
        db.commit()
    return _redirect(f"/admin/rounds/{round_id}")


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------
@router.post("/rounds/{round_id}/picks/{participant_id}")
def set_pick(
    round_id: int,
    participant_id: int,
    request: Request,
    tmdb_id: int = Form(...),
    db: Session = Depends(get_db),
):
    round_ = _get_round(db, round_id)
    try:
        movie = upsert_movie_from_tmdb(db, tmdb_id)
    except TMDBError as exc:
        db.rollback()
        flash(request, f"Could not load TMDB {tmdb_id}: {exc}", "error")
        return _redirect(f"/admin/rounds/{round_id}")

    pick = db.scalar(
        select(Pick).where(
            Pick.round_id == round_.id, Pick.participant_id == participant_id
        )
    )
    if pick is None:
        db.add(
            Pick(
                round_id=round_.id,
                participant_id=participant_id,
                movie_id=movie.id,
            )
        )
    else:
        pick.movie_id = movie.id
    db.commit()
    flash(request, f"Set {movie.title} ({movie.year}).")
    return _redirect(f"/admin/rounds/{round_id}")


@router.post("/rounds/{round_id}/picks/{participant_id}/delete")
def clear_pick(
    round_id: int,
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    pick = db.scalar(
        select(Pick).where(
            Pick.round_id == round_id, Pick.participant_id == participant_id
        )
    )
    if pick:
        db.delete(pick)
        db.commit()
        flash(request, "Pick cleared.")
    return _redirect(f"/admin/rounds/{round_id}")


# ---------------------------------------------------------------------------
# Manual sync triggers
# ---------------------------------------------------------------------------
@router.post("/rounds/{round_id}/sync")
def sync_now(round_id: int, request: Request, db: Session = Depends(get_db)):
    round_ = _get_round(db, round_id)
    if not settings.trakt_enabled:
        flash(request, "TRAKT_CLIENT_ID is not configured.", "error")
        return _redirect(f"/admin/rounds/{round_id}")

    entry = SyncLog(kind="trakt-manual", round_number=round_.number)
    db.add(entry)
    try:
        result = sync_round(db, round_, TraktClient())
    except Exception as exc:
        log.exception("Manual sync failed for round %s", round_.number)
        db.rollback()
        entry = SyncLog(
            kind="trakt-manual", round_number=round_.number, ok=False,
            message=str(exc)[:1000],
        )
        db.add(entry)
        db.commit()
        flash(request, f"Sync failed: {exc}", "error")
        return _redirect(f"/admin/rounds/{round_id}")

    entry.ok = result.ok
    entry.watches_added = result.watches_added
    entry.ratings_added = result.ratings_added
    entry.message = result.summary()[:1000]
    from app.models import utcnow

    entry.finished_at = utcnow()
    db.commit()
    flash(request, f"Trakt sync: {result.summary()}", "ok" if result.ok else "error")
    return _redirect(f"/admin/rounds/{round_id}")


@router.post("/rounds/{round_id}/refresh-metadata")
def refresh_metadata(round_id: int, request: Request, db: Session = Depends(get_db)):
    round_ = _get_round(db, round_id)
    picks = db.scalars(select(Pick).where(Pick.round_id == round_.id)).all()
    client = TMDBClient()
    refreshed, failures = 0, []
    for pick in picks:
        movie = pick.movie
        try:
            upsert_movie_from_tmdb(db, movie.tmdb_id, client=client, refresh=True)
            refreshed += 1
        except TMDBError as exc:
            failures.append(f"{movie.title}: {exc}")
    db.commit()

    if failures:
        flash(request, f"Refreshed {refreshed}. Errors: {'; '.join(failures)}", "error")
    else:
        flash(request, f"Refreshed TMDB metadata for {refreshed} film(s).")
    return _redirect(f"/admin/rounds/{round_id}")
