"""Shared pytest fixtures.

Each test gets a fresh, isolated in-memory-ish SQLite DB (a temp file) plus a
controllable clock so the 24h withdrawal rule is testable without waiting.
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.database import Database
from app.container import Container


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def app(tmp_path, clock):
    db = Database(tmp_path / "test.db")
    container = Container(db=db, now_fn=clock)
    for b in ("brand_1", "brand_2", "brand_3"):
        container.create_brand(b)
    yield container
    db.close()


@pytest.fixture
def user(app):
    app.create_user("john_doe")
    return "john_doe"
