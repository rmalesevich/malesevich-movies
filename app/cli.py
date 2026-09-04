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
    """Show the participant roster."""
    db = SessionLocal()
    try:
        people = db.scalars(
            select(Participant).order_by(Participant.joined_round, Participant.name)
        ).all()
        if not people:
            typer.echo("No participants yet.")
            return
        for person in people:
            window = f"rounds {person.joined_round}-{person.left_round or 'now'}"
            trakt = person.trakt_username or "no trakt account"
            typer.echo(f"  {person.name:<20} {window:<18} {trakt}")
    finally:
        db.close()


if __name__ == "__main__":
    cli()
