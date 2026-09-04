"""Single shared-password login.

There is exactly one user, so there is no user table - just a signed session
cookie set after the password check. Setting APP_PASSWORD to an empty string
disables the wall entirely.
"""
from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

SESSION_KEY = "authenticated"

# Paths reachable without a session.
PUBLIC_PREFIXES = ("/login", "/static", "/healthz")


def password_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", settings.app_password)


def is_authenticated(request: Request) -> bool:
    if not settings.auth_enabled:
        return True
    return bool(request.session.get(SESSION_KEY))


def sign_in(request: Request) -> None:
    request.session[SESSION_KEY] = True


def sign_out(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirects unauthenticated browsers to the login page."""

    async def dispatch(self, request: Request, call_next):
        if not settings.auth_enabled:
            return await call_next(request)
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES) or is_authenticated(request):
            return await call_next(request)
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        target = "/login"
        if path != "/":
            target = f"/login?next={request.url.path}"
        return RedirectResponse(target, status_code=303)
