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


# ---- the failure path, which the smoke tests above never reach ----

def test_a_wrapped_rate_limit_is_explained_not_reported_as_a_crash():
    """
    Regression from the deployed demo.

    A rate limit reaches the UI as an ExceptionGroup whose own message says
    nothing about rate limits, so matching on str(exc) showed a plain "try again
    later" as an unexplained failure.
    """
    import app

    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [Exception("Rate limit reached for model")]
    )
    assert "rate limited" in app.explain_failure(group).lower()


def test_a_wrapped_auth_error_names_the_key():
    import app

    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [Exception("invalid api_key provided")]
    )
    assert "key" in app.explain_failure(group).lower()


def test_an_unknown_failure_names_the_innermost_type_not_the_group():
    """`ExceptionGroup` on its own tells the reader nothing, including me."""
    import app

    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [KeyError("nope")])
    message = app.explain_failure(group)
    assert "KeyError" in message and "ExceptionGroup" not in message


def test_failure_messages_contain_no_raw_html():
    """st.markdown escapes HTML, so a <sub> wrapper rendered as literal text."""
    import app

    message = app.explain_failure(BaseExceptionGroup("g", [KeyError("x")]))
    assert "<" not in message and ">" not in message


def test_a_failed_question_shows_the_explanation_and_no_trajectory_note(monkeypatch):
    """
    End to end through the real script: submit a question, make the agent fail,
    and check what the visitor sees.

    The earlier smoke tests never submitted anything, so the whole error path —
    the path the deployed demo actually hit — went untested.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_used")

    async def boom(*args, **kwargs):
        raise BaseExceptionGroup(
            "unhandled errors in a TaskGroup", [Exception("Rate limit reached")]
        )

    monkeypatch.setattr("agent.graph.build_agent", boom)

    at = AppTest.from_file(APP, default_timeout=60).run()
    at.chat_input[0].set_value("What does a churro bar cost?").run()

    assert list(at.exception) == []
    body = " ".join(m.value for m in at.markdown)
    assert "rate limited" in body.lower()
    # "No tools were called" after a crash reads as a second, invented problem.
    assert "no tools were called" not in body.lower()
