"""
Shared fixtures: an isolated database and a frozen clock.

Every test runs against its own temporary SQLite file and a fixed "today", so
the suite gives the same answer in January as in July. Booking rules are all
relative to the current date — a suite that used the real clock would start
failing on whichever weekday the seed data happens to close.
"""
import asyncio
from datetime import date, timedelta

import pytest

from ops import store

# A Tuesday. Chosen so the fixture's "next Monday" is genuinely closed and the
# nearby Saturdays carry the seeded bookings.
FROZEN_TODAY = date(2026, 9, 1)


def next_weekday(weekday: int, *, after_days: int = 0, start: date = FROZEN_TODAY) -> str:
    """
    The next date with the given weekday (Mon=0), at least `after_days` out.

    Tests ask for "a Saturday far enough ahead to clear the lead time" rather
    than hardcoding a date, so the seed window can move without breaking them.
    """
    day = start + timedelta(days=after_days)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day.isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A seeded database, isolated per test."""
    path = str(tmp_path / "ops.db")
    monkeypatch.setattr(store, "DEFAULT_DB", path)
    store.seed(db_path=path, today=FROZEN_TODAY, days=60)
    return path


@pytest.fixture
def tools(db, monkeypatch):
    """
    The MCP server, wired to the isolated database and the frozen clock.

    Returns a `call(name, **kwargs)` helper that runs a tool and hands back its
    structured payload — so tests exercise the real registered tool, schema
    validation and all, rather than the underlying Python function.
    """
    import server as srv

    monkeypatch.setattr(srv, "_today", lambda: FROZEN_TODAY)

    def call(name: str, **kwargs):
        result = asyncio.run(srv.server.call_tool(name, kwargs))
        payload = result.structured_content
        # The SDK wraps a dict return under "result"; unwrap so tests read the
        # shape the tool actually returns.
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    return call
