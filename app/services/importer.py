"""Seed historical rounds from a CSV export of the spreadsheet.

Expected columns (header row required, extra columns are ignored):

    round, started_on, discussed_on, participant, tmdb_id, title, year, notes

``tmdb_id`` is optional - when it is blank the film is resolved by searching
TMDB for ``title`` (narrowed by ``year`` when present). ``discussed_on`` is
also optional: a round with no discussion date inherits the start date of the
following round, which is how the project actually runs.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Movie,
    Participant,
    Pick,
    Round,
    RoundParticipant,
    RoundStatus,
)
from app.services.participants import clean_name, name_index, normalize_name
from app.services.tmdb import TMDBClient, TMDBError, upsert_movie_from_tmdb

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"round", "participant"}


@dataclass
class ImportReport:
    rounds_created: int = 0
    participants_created: int = 0
    movies_created: int = 0
    picks_created: int = 0
    skipped: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"rounds created:       {self.rounds_created}",
            f"participants created: {self.participants_created}",
            f"movies imported:      {self.movies_created}",
            f"picks created:        {self.picks_created}",
        ]
        if self.unresolved:
            lines.append(f"unresolved films:     {len(self.unresolved)}")
            lines.extend(f"  ! {item}" for item in self.unresolved)
        if self.skipped:
            lines.append(f"skipped rows:         {len(self.skipped)}")
            lines.extend(f"  - {item}" for item in self.skipped[:20])
        return "\n".join(lines)


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d")


def _parse_date(value: str | None) -> date | None:
    """Accept the handful of date formats a spreadsheet export tends to emit."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {value!r}")


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV file has no header row")
        headers = {(name or "").strip().lower() for name in reader.fieldnames}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(f"CSV is missing required column(s): {sorted(missing)}")
        rows = []
        for raw in reader:
            rows.append(
                {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            )
        return rows


def resolve_tmdb_id(
    client: TMDBClient, title: str, year: int | None
) -> tuple[int | None, str | None]:
    """Best-effort title -> TMDB id lookup. Returns (id, note)."""
    results = client.search_movies(title)
    if not results:
        return None, f"no TMDB match for {title!r}"

    def score(item: dict[str, Any]) -> tuple[int, int, float]:
        item_year = None
        if item.get("release_date"):
            try:
                item_year = int(item["release_date"][:4])
            except ValueError:
                item_year = None
        exact_title = (item.get("title") or "").lower() == title.lower()
        year_match = year is not None and item_year == year
        return (int(year_match), int(exact_title), item.get("popularity") or 0.0)

    best = max(results, key=score)
    best_year = (best.get("release_date") or "")[:4]
    note = None
    if year and best_year and int(best_year) != year:
        note = (
            f"{title} ({year}) matched TMDB {best['id']} "
            f"'{best.get('title')}' ({best_year}) - verify"
        )
    return best["id"], note


def _get_or_create_participant(
    db: Session,
    name: str,
    round_number: int,
    report: ImportReport,
    index: dict[str, Participant],
) -> Participant:
    """Find the participant this row refers to, creating them only if new.

    Matching ignores capitalisation and stray spacing, so "ryan", "Ryan" and
    "Ryan " in the same spreadsheet all resolve to one person. ``index`` is
    kept in step as rows are processed so a name first seen mid-file is not
    created twice.
    """
    key = normalize_name(name)
    participant = index.get(key)
    if participant is None:
        # Stored with the spelling the CSV used, minus the stray spacing.
        participant = Participant(name=clean_name(name), joined_round=round_number)
        db.add(participant)
        db.flush()
        index[key] = participant
        report.participants_created += 1
    elif round_number < participant.joined_round:
        participant.joined_round = round_number
    return participant


def import_rounds(
    db: Session,
    path: Path,
    client: TMDBClient | None = None,
    close_all: bool = True,
    dry_run: bool = False,
) -> ImportReport:
    """Import historical rounds. Existing rounds and picks are left alone."""
    report = ImportReport()
    client = client or TMDBClient()
    rows = read_rows(path)
    # Existing participants, keyed by normalised name. Built once rather than
    # queried per row, and updated as new people are created.
    people = name_index(db)

    grouped: dict[int, list[dict[str, str]]] = {}
    for index, row in enumerate(rows, start=2):
        number = _int_or_none(row.get("round"))
        if number is None:
            report.skipped.append(f"line {index}: unreadable round number")
            continue
        grouped.setdefault(number, []).append({**row, "_line": str(index)})

    ordered = sorted(grouped)
    # Round N's discussion date defaults to the day before round N+1 starts.
    start_dates: dict[int, date | None] = {}
    for number in ordered:
        for row in grouped[number]:
            parsed = _parse_date(row.get("started_on")) if row.get("started_on") else None
            if parsed:
                start_dates[number] = parsed
                break
        start_dates.setdefault(number, None)

    for position, number in enumerate(ordered):
        round_rows = grouped[number]
        existing = db.scalar(select(Round).where(Round.number == number))
        if existing is not None:
            report.skipped.append(f"round {number}: already exists, left untouched")
            continue

        started_on = start_dates.get(number)
        if started_on is None:
            report.skipped.append(f"round {number}: no start date, skipped")
            continue

        discussed_on = None
        for row in round_rows:
            if row.get("discussed_on"):
                discussed_on = _parse_date(row["discussed_on"])
                break
        if discussed_on is None and position + 1 < len(ordered):
            next_start = start_dates.get(ordered[position + 1])
            if next_start:
                discussed_on = next_start - timedelta(days=1)

        is_last = position == len(ordered) - 1
        status = (
            RoundStatus.OPEN
            if (is_last and not close_all and discussed_on is None)
            else RoundStatus.CLOSED
        )
        if status == RoundStatus.CLOSED and discussed_on is None:
            discussed_on = started_on

        round_ = Round(
            number=number,
            started_on=started_on,
            discussed_on=discussed_on,
            status=status,
        )
        db.add(round_)
        db.flush()
        report.rounds_created += 1

        for row in round_rows:
            line = row.get("_line", "?")
            name = row.get("participant", "")
            if not name:
                report.skipped.append(f"line {line}: missing participant")
                continue
            participant = _get_or_create_participant(
                db, name, number, report, people
            )

            if not db.scalar(
                select(RoundParticipant).where(
                    RoundParticipant.round_id == round_.id,
                    RoundParticipant.participant_id == participant.id,
                )
            ):
                db.add(
                    RoundParticipant(
                        round_id=round_.id, participant_id=participant.id
                    )
                )

            title = row.get("title", "")
            tmdb_id = _int_or_none(row.get("tmdb_id"))
            year = _int_or_none(row.get("year"))
            if tmdb_id is None:
                if not title:
                    report.skipped.append(f"line {line}: no tmdb_id and no title")
                    continue
                try:
                    tmdb_id, note = resolve_tmdb_id(client, title, year)
                except TMDBError as exc:
                    report.unresolved.append(f"line {line}: {title} - {exc}")
                    continue
                if note:
                    report.unresolved.append(f"line {line}: {note}")
                if tmdb_id is None:
                    report.unresolved.append(f"line {line}: could not resolve {title!r}")
                    continue

            known = db.scalar(select(Movie).where(Movie.tmdb_id == tmdb_id))
            try:
                movie = upsert_movie_from_tmdb(db, tmdb_id, client=client)
            except TMDBError as exc:
                report.unresolved.append(f"line {line}: TMDB {tmdb_id} - {exc}")
                continue
            if known is None:
                report.movies_created += 1

            if not db.scalar(
                select(Pick).where(
                    Pick.round_id == round_.id, Pick.participant_id == participant.id
                )
            ):
                db.add(
                    Pick(
                        round_id=round_.id,
                        participant_id=participant.id,
                        movie_id=movie.id,
                        notes=row.get("notes") or None,
                    )
                )
                report.picks_created += 1

        db.flush()

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return report
