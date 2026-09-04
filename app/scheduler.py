"""Daily background job that refreshes Trakt data for the open round(s)."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.services.sync import sync_open_rounds

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def run_daily_sync() -> None:
    """Entry point for the scheduled job (and the `sync` CLI command)."""
    db = SessionLocal()
    try:
        result = sync_open_rounds(db)
        log.info("Daily Trakt sync finished: %s", result.summary())
    except Exception:
        log.exception("Daily Trakt sync raised")
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    """Start the scheduler unless it is disabled or Trakt is unconfigured.

    Runs in-process, so the app must be served with a single worker - which is
    the right shape for this anyway.
    """
    global _scheduler

    if not settings.sync_enabled:
        log.info("SYNC_ENABLED is false - scheduler not started")
        return None
    if not settings.trakt_enabled:
        log.warning("TRAKT_CLIENT_ID is not set - scheduler not started")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=settings.timezone)
    _scheduler.add_job(
        run_daily_sync,
        CronTrigger(hour=settings.sync_hour, minute=settings.sync_minute),
        id="daily-trakt-sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
    )
    _scheduler.start()
    log.info(
        "Trakt sync scheduled daily at %02d:%02d %s",
        settings.sync_hour,
        settings.sync_minute,
        settings.timezone,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
