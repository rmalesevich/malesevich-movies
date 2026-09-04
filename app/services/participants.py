"""Identity rules for participants.

There are four people in this project and their names get typed by hand into a
spreadsheet, so "ryan", "Ryan" and "Ryan " are the same person. Matching goes
through here so the importer, the admin form and the CLI all agree on that.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Participant, Pick, Rating, RoundParticipant, Watch


def clean_name(name: str) -> str:
    """The form a name is *stored* in: outer and repeated whitespace removed,
    capitalisation left exactly as the person typed it."""
    return " ".join((name or "").split())


def normalize_name(name: str) -> str:
    """The form a name is *compared* in. Never stored.

    casefold() rather than lower() so non-ASCII spellings compare correctly.
    """
    return clean_name(name).casefold()


def name_index(db: Session) -> dict[str, Participant]:
    """Every participant keyed by normalised name, for repeated lookups.

    If a database already contains duplicates ("Ryan" and "ryan" both created
    before matching was loosened) the earliest record wins, so behaviour stays
    predictable until they are merged.
    """
    index: dict[str, Participant] = {}
    for participant in db.scalars(
        select(Participant).order_by(Participant.id)
    ).all():
        index.setdefault(normalize_name(participant.name), participant)
    return index


def resolve(db: Session, name: str) -> Participant | None:
    """Like :func:`find_by_name`, but an exact spelling wins first.

    Needed to tell existing duplicates apart: with both "Ryan" and "ryan" in
    the table, a loose lookup collapses them onto one record, which would make
    them impossible to merge.
    """
    exact = db.scalar(select(Participant).where(Participant.name == name))
    if exact is not None:
        return exact
    return find_by_name(db, name)


def find_by_name(db: Session, name: str) -> Participant | None:
    """Case- and spacing-insensitive lookup of a single participant."""
    return name_index(db).get(normalize_name(name))


def merge(db: Session, source: Participant, target: Participant) -> dict[str, int]:
    """Fold ``source`` into ``target`` and delete it.

    Used to repair duplicates created before names were matched loosely. Every
    row that points at ``source`` is repointed at ``target``, except where that
    would collide with a row target already has - those are dropped, since the
    two records describe the same person doing the same thing.
    """
    moved = {"picks": 0, "watches": 0, "ratings": 0, "rounds": 0, "dropped": 0}

    # Round membership: unique(round_id, participant_id).
    target_rounds = {
        rp.round_id
        for rp in db.scalars(
            select(RoundParticipant).where(
                RoundParticipant.participant_id == target.id
            )
        ).all()
    }
    for rp in db.scalars(
        select(RoundParticipant).where(RoundParticipant.participant_id == source.id)
    ).all():
        if rp.round_id in target_rounds:
            db.delete(rp)
            moved["dropped"] += 1
        else:
            rp.participant_id = target.id
            moved["rounds"] += 1

    # Picks: unique(round_id, participant_id).
    target_picks = {
        p.round_id
        for p in db.scalars(
            select(Pick).where(Pick.participant_id == target.id)
        ).all()
    }
    for pick in db.scalars(
        select(Pick).where(Pick.participant_id == source.id)
    ).all():
        if pick.round_id in target_picks:
            db.delete(pick)
            moved["dropped"] += 1
        else:
            pick.participant_id = target.id
            moved["picks"] += 1

    # Watches and ratings: unique(round_id, participant_id, movie_id).
    for model, key in ((Watch, "watches"), (Rating, "ratings")):
        existing = {
            (row.round_id, row.movie_id)
            for row in db.scalars(
                select(model).where(model.participant_id == target.id)
            ).all()
        }
        for row in db.scalars(
            select(model).where(model.participant_id == source.id)
        ).all():
            if (row.round_id, row.movie_id) in existing:
                db.delete(row)
                moved["dropped"] += 1
            else:
                row.participant_id = target.id
                moved[key] += 1

    # Keep the widest participation window of the two.
    target.joined_round = min(target.joined_round, source.joined_round)
    if target.left_round is not None:
        target.left_round = (
            None if source.left_round is None
            else max(target.left_round, source.left_round)
        )
    if not target.trakt_username and source.trakt_username:
        target.trakt_username = source.trakt_username

    # The rows above were repointed by mutating ORM objects, but `source` still
    # holds them in its loaded `picks` / `memberships` collections. Deleting it
    # without expiring first makes SQLAlchemy null out those foreign keys and
    # undo the move. Expire so the delete re-reads and finds no children.
    db.flush()
    db.expire(source)
    db.delete(source)
    db.flush()
    return moved
