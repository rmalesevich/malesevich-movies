"""Jinja environment plus the small helpers every template expects."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import SessionLocal
from app.services.rounds import tagline as build_tagline
from app.services.tmdb import poster_url

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["poster_url"] = poster_url

FLASH_KEY = "_flashes"


def flash(request: Request, message: str, category: str = "ok") -> None:
    request.session.setdefault(FLASH_KEY, []).append([category, message])


def pop_flashes(request: Request) -> list[tuple[str, str]]:
    messages = request.session.pop(FLASH_KEY, [])
    return [tuple(item) for item in messages]


def _tagline() -> str | None:
    """Derived from the data, so it needs a session of its own.

    Every page extends base.html, so computing it here keeps each view from
    having to remember to pass it. It is two aggregates against a local SQLite
    file, which is cheap enough to do per render.
    """
    db = SessionLocal()
    try:
        return build_tagline(db)
    finally:
        db.close()


def render(request: Request, template: str, context: dict[str, Any] | None = None):
    payload: dict[str, Any] = {
        "request": request,
        "flashes": pop_flashes(request),
        "tagline": _tagline(),
        "auth_enabled": settings.auth_enabled,
        "tmdb_enabled": settings.tmdb_enabled,
        "trakt_enabled": settings.trakt_enabled,
        "nav": None,
    }
    payload.update(context or {})
    return templates.TemplateResponse(request, template, payload)
