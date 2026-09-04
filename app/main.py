"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import is_authenticated, password_matches, sign_in, sign_out
from app.config import BASE_DIR, settings
from app.routers import admin, api, views
from app.scheduler import start_scheduler, stop_scheduler
from app.templating import templates

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)

# SessionMiddleware has to be added before AuthMiddleware so that the auth
# layer can read request.session (middleware runs in reverse order of addition).
from app.auth import AuthMiddleware  # noqa: E402  (import after app for clarity)

app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=False,
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "static")),
    name="static",
)

app.include_router(views.router)
app.include_router(admin.router)
app.include_router(api.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if not settings.auth_enabled or is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next_url": next, "error": None,
                                "app_name": settings.app_name}
    )


@app.post("/login")
def login_submit(
    request: Request, password: str = Form(""), next: str = Form("/")
):
    if password_matches(password):
        sign_in(request)
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next_url": next, "error": "Incorrect password.",
         "app_name": settings.app_name},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    sign_out(request)
    return RedirectResponse("/login", status_code=303)
