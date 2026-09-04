"""Command line utilities: `python -m app.cli --help`."""
from __future__ import annotations

import logging
from pathlib import Path

import typer
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Participant, Round
from app.services.importer import import_rounds
from app.services.participants import merge, normalize_name, resolve
from app.services.sync import sync_open_rounds, sync_round
from app.services.tmdb import TMDBClient, upsert_movie_from_tmdb
from app.services.trakt import TraktClient

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

cli = typer.Typer(help="Malesevich Movies maintenance commands", no_args_is_help=True)


@cli.command("import-history")
def import_history(
    path: Path = typer.Argument(..., exists=True, readable=True,
                                help="CSV of historical rounds"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing"),
    sync_trakt: bool = typer.Option(
        False, "--sync-trakt",
        help="After importing, pull Trakt history for each imported round",
    ),
):
    """Seed historical rounds from a CSV and pull their TMDB metadata."""
    if not settings.tmdb_enabled:
        typer.secho("TMDB_API_KEY is not set.", fg="red")
        raise typer.Exit(1)

    db = SessionLocal()
    try:
        report = import_rounds(db, path, TMDBClient(), dry_run=dry_run)
        typer.echo(report.summary())
        if dry_run:
            typer.secho("\nDry run - nothing was written.", fg="yellow")
            return

        if sync_trakt:
            if not settings.trakt_enabled:
                typer.secho("TRAKT_CLIENT_ID is not set - skipping Trakt.", fg="yellow")
                return
            client = TraktClient()
            for round_ in db.scalars(select(Round).order_by(Round.number)).all():
                result = sync_round(db, round_, client)
                db.commit()
                typer.echo(f"round {round_.number}: {result.summary()}")
    finally:
        db.close()


@cli.command("sync")
def sync(
    round_number: int = typer.Option(
        None, "--round", help="Sync one round instead of every open round"
    ),
):
    """Pull Trakt watch and rating history."""
    if not settings.trakt_enabled:
        typer.secho("TRAKT_CLIENT_ID is not set.", fg="red")
        raise typer.Exit(1)

    db = SessionLocal()
    try:
        if round_number is None:
            result = sync_open_rounds(db)
        else:
            round_ = db.scalar(select(Round).where(Round.number == round_number))
            if round_ is None:
                typer.secho(f"Round {round_number} not found.", fg="red")
                raise typer.Exit(1)
            result = sync_round(db, round_, TraktClient())
            db.commit()
        typer.echo(result.summary())
        if not result.ok:
            raise typer.Exit(1)
    finally:
        db.close()


@cli.command("refresh-metadata")
def refresh_metadata(
    all_movies: bool = typer.Option(
        False, "--all", help="Re-fetch every film, not just ones missing metadata"
    ),
):
    """Re-pull TMDB details, cast and crew for stored films."""
    from app.models import Movie

    db = SessionLocal()
    try:
        query = select(Movie).order_by(Movie.title)
        if not all_movies:
            query = query.where(Movie.metadata_synced_at.is_(None))
        movies = db.scalars(query).all()
        client = TMDBClient()
        for movie in movies:
            upsert_movie_from_tmdb(db, movie.tmdb_id, client=client, refresh=True)
            db.commit()
            typer.echo(f"  {movie.title}")
        typer.echo(f"Refreshed {len(movies)} film(s).")
    finally:
        db.close()


@cli.command("participants")
def list_participants():
    """Show the participant roster, flagging likely duplicates."""
    db = SessionLocal()
    try:
        people = db.scalars(
            select(Participant).order_by(Participant.joined_round, Participant.name)
        ).all()
        if not people:
            typer.echo("No participants yet.")
            return

        by_key: dict[str, list[Participant]] = {}
        for person in people:
            by_key.setdefault(normalize_name(person.name), []).append(person)

        for person in people:
            window = f"rounds {person.joined_round}-{person.left_round or 'now'}"
            trakt = person.trakt_username or "no trakt account"
            typer.echo(f"  {person.name:<20} {window:<18} {trakt}")

        dupes = [group for group in by_key.values() if len(group) > 1]
        if dupes:
            typer.echo("")
            typer.secho(
                "Names that differ only by case or spacing:", fg="yellow"
            )
            for group in dupes:
                names = " / ".join(repr(p.name) for p in group)
                typer.echo(f"  {names}")
            typer.echo(
                "\nFold them together with:\n"
                "  python -m app.cli merge-participants <from> <into>"
            )
    finally:
        db.close()


@cli.command("merge-participants")
def merge_participants(
    source: str = typer.Argument(..., help="The duplicate to remove"),
    target: str = typer.Argument(..., help="The participant to keep"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing"),
):
    """Fold one participant into another, moving their picks and history.

    Repairs duplicates created before names were matched loosely, e.g.
    `merge-participants ryan Ryan`. Rows that would collide (both records
    picking in the same round) are dropped rather than duplicated.
    """
    db = SessionLocal()
    try:
        src = resolve(db, source)
        dst = resolve(db, target)
        if src is None:
            typer.secho(f"No participant matching {source!r}.", fg="red")
            raise typer.Exit(1)
        if dst is None:
            typer.secho(f"No participant matching {target!r}.", fg="red")
            raise typer.Exit(1)
        if src.id == dst.id:
            typer.secho(
                f"{source!r} and {target!r} are already the same record.", fg="yellow"
            )
            raise typer.Exit(1)

        moved = merge(db, src, dst)
        typer.echo(
            f"Folded {src.name!r} into {dst.name!r}: "
            f"{moved['picks']} picks, {moved['watches']} watches, "
            f"{moved['ratings']} ratings, {moved['rounds']} round memberships"
            + (f", {moved['dropped']} duplicate rows dropped" if moved["dropped"] else "")
        )
        if dry_run:
            db.rollback()
            typer.secho("Dry run - nothing was written.", fg="yellow")
        else:
            db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    cli()
