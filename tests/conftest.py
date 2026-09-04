"""Test fixtures.

The app builds its engine at import time from settings, so the environment has
to be pointed at a throwaway database *before* anything under ``app`` loads.
"""
import os
import tempfile
from pathlib import Path

import pytest

TMP_DB = Path(tempfile.mkdtemp(prefix="malesevich-tests-")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB}"
os.environ["APP_PASSWORD"] = ""          # no login wall in tests
os.environ["SYNC_ENABLED"] = "false"     # no background scheduler
os.environ["TMDB_API_KEY"] = ""
os.environ["TRAKT_CLIENT_ID"] = ""
os.environ["SECRET_KEY"] = "test-secret"

import app.models  # noqa: E402,F401
from app.db import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    """Each test starts from an empty database.

    Fixtures commit, so rolling a transaction back is not enough - the rows
    have to be deleted between tests.
    """
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
