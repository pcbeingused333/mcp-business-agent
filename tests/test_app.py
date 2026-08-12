"""
Smoke tests for the Streamlit demo, run headlessly with Streamlit's AppTest.

These exist because "the server starts" is not the same as "the script runs".
A Streamlit app can boot cleanly and then throw on the first browser connection,
which is exactly when a recruiter opens it. AppTest executes the script body, so
an import error, a bad session-state access or a failed seed fails here instead.

No model is called: the first run renders suggestions and waits for input.
"""
import os

import pytest

from ops import store

pytest.importorskip("streamlit.testing.v1", reason="streamlit not installed")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


@pytest.fixture
def app(monkeypatch):
    # The app stops early without a key; the tests below never reach the model.
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_used")
    return AppTest.from_file(APP, default_timeout=60).run()


def test_the_script_runs_without_raising(app):
    assert list(app.exception) == []
    assert [e.value for e in app.error] == []


def test_the_first_screen_offers_something_to_click(app):
    """A visitor who does not know what to ask should not face an empty box."""
    assert app.title[0].value.endswith("Business Operations Agent")
    assert len(app.button) >= 3


def test_each_session_seeds_its_own_database(app):
    """
    The failure this guards against: place_order permanently consumes capacity.

    On one shared database, every booking a visitor makes leaves less room for
    the next, and the demo eventually answers "no availability" to everything.
    """
    path = app.session_state["db_path"]
    assert os.path.exists(path)
    assert len(store.list_products(db_path=path)) > 0
    assert len(store.open_days(db_path=path)) > 0


def test_two_sessions_do_not_share_a_database(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_used")
    first = AppTest.from_file(APP, default_timeout=60).run()
    second = AppTest.from_file(APP, default_timeout=60).run()
    assert first.session_state["db_path"] != second.session_state["db_path"]


def test_a_missing_api_key_is_explained_rather_than_crashing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    # A .env on the developer's machine would defeat the point of this test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    result = AppTest.from_file(APP, default_timeout=60).run()
    assert list(result.exception) == []
    assert any("GROQ_API_KEY" in e.value for e in result.error)
