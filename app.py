"""
Streamlit demo: the agent, with its tool trajectory shown as it works.

The trajectory is the point of the demo. A chat window that answers correctly
proves nothing about an agent — the interesting question is which tools it
reached for, in what order, and whether the numbers it quotes came back from one
of them. So every call and result is rendered next to the answer.

Self-contained by design: SQLite seeded on first boot, no external database, no
embedding model. The only thing it needs is a Groq API key. That is deliberate —
a demo whose database can pause is a demo that is dead exactly when someone
opens it.
"""
import asyncio
import os
import tempfile

import streamlit as st

st.set_page_config(page_title="Business Ops Agent (MCP)", page_icon="🥨", layout="wide")

# Bridge Streamlit Cloud secrets into the environment before anything reads
# os.getenv. Harmless locally: with no secrets.toml this raises and falls back
# to the .env file.
try:
    for _key, _value in st.secrets.items():
        os.environ.setdefault(_key, str(_value))
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv()

from mcp import Client

from ops import store
import server as srv

SUGGESTIONS = [
    "What does a churro bar cost per person?",
    "Can you do catering for 80 people this Saturday? We'd want delivery.",
    "We need catering for 20 people next Friday.",
    "¿Tenéis hueco para 60 personas el próximo sábado?",
]


def prepare_database() -> str:
    """
    A fresh, private database per visitor.

    `place_order` permanently consumes a day's capacity. On one shared database
    every booking a visitor makes leaves less room for the next, and after a
    handful of visits the demo answers "no availability" to everything and looks
    broken — the exact failure this project keeps warning about.

    A per-session file in the system temp directory fixes that and, incidentally,
    removes any assumption that the deploy's working directory is writable.
    Sessions are cleaned up by the OS; the file is a few tens of KB.
    """
    if "db_path" not in st.session_state:
        handle = tempfile.NamedTemporaryFile(prefix="ops-demo-", suffix=".db", delete=False)
        handle.close()
        st.session_state.db_path = handle.name
        store.DEFAULT_DB = handle.name
        store.seed(db_path=handle.name)
    # Rebind on every rerun: Streamlit re-executes this module per interaction,
    # and the module-level default would otherwise point at the repo directory.
    store.DEFAULT_DB = st.session_state.db_path
    return st.session_state.db_path


def explain_failure(exc: BaseException) -> str:
    """
    Turn a failure into something a visitor can act on.

    The classification looks at every exception nested inside the failure, not
    just the outermost one. MCP and LangGraph run tool calls in anyio task
    groups, so a rate limit reaches this handler as an ExceptionGroup whose own
    message says nothing about rate limits — matching on `str(exc)` reported a
    plain "try again in a minute" as an unexplained crash, which is what the
    deployed demo did before this.

    The type name is rendered as backticked text rather than an HTML tag:
    st.markdown escapes HTML by default, so a `<sub>` wrapper appeared on screen
    as literal angle brackets.
    """
    from agent.graph import describe

    detail = describe(exc)
    if "rate limit" in detail or "429" in detail:
        return (
            "⚠️ **The model is rate limited right now.** This demo runs on Groq's "
            "free tier, which has a daily token cap. Try again in a few minutes."
        )
    if "api_key" in detail or "authentication" in detail or "401" in detail:
        return "⚠️ **The Groq API key was rejected.** Check the app's secrets."
    if "overloaded" in detail or "503" in detail:
        return "⚠️ **The model is overloaded.** Try again shortly."

    # Show the innermost concrete type: ExceptionGroup on its own tells nobody
    # anything, including me.
    from agent.graph import unwrap

    inner = [e for e in unwrap(exc) if not isinstance(e, BaseExceptionGroup)]
    name = type(inner[-1] if inner else exc).__name__
    return f"⚠️ **The agent failed to answer.** (`{name}`)"


async def answer(question: str):
    """Run one question and return (final answer, [(tool, args, result), …])."""
    from agent.graph import build_agent
    from langchain_core.messages import HumanMessage

    async with Client(srv.server) as client:
        agent = await build_agent(client)
        state = await agent.ainvoke({"messages": [HumanMessage(content=question)]})

    messages = state["messages"]
    pending, steps = [], []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            pending.append((call["name"], call.get("args") or {}))
        if message.__class__.__name__ == "ToolMessage" and pending:
            name, args = pending.pop(0)
            steps.append((name, args, str(message.content)))
    return messages[-1].content, steps


prepare_database()

st.title("🥨 Business Operations Agent")
st.caption(
    "A LangGraph agent that can only act through an MCP server. "
    "Every tool call it makes is shown below the answer."
)
st.caption(
    "Your session gets its own copy of the business, so bookings you make here "
    "do not affect anyone else's demo."
)

with st.sidebar:
    st.subheader("How it works")
    st.markdown(
        "The agent holds **no business rules**. It does not know the catering "
        "minimum or the lead time — the `quote_catering` tool tells it, and the "
        "same tools work unchanged in Claude Desktop or Cursor.\n\n"
        "**Tools available**\n"
        "- `list_catalog` — products and prices\n"
        "- `check_availability` — capacity for a date\n"
        "- `check_stock` — on-hand quantities\n"
        "- `quote_catering` — price + every booking rule at once\n"
        "- `place_order` — books it *(the only write)*\n"
        "- `lookup_order` — retrieve a booking\n"
    )
    st.divider()
    st.markdown(
        "[Source on GitHub]"
        "(https://github.com/pcbeingused333/mcp-business-agent)"
    )

if not os.getenv("GROQ_API_KEY"):
    st.error(
        "GROQ_API_KEY is not set. Add it to the app's secrets "
        "(free key at console.groq.com/keys)."
    )
    st.stop()

if "history" not in st.session_state:
    st.session_state.history = []

question = st.chat_input("Ask about prices, availability, or a catering job…")

if not st.session_state.history:
    st.info(
        "The demo business is a churrería with a live catalog, daily booking "
        "capacity and stock. Ask it something, or try one of these:"
    )
    columns = st.columns(2)
    for index, suggestion in enumerate(SUGGESTIONS):
        if columns[index % 2].button(suggestion, key=f"s{index}", use_container_width=True):
            question = suggestion

for entry in st.session_state.history:
    with st.chat_message(entry["role"]):
        st.markdown(entry["content"])
        for name, args, result in entry.get("steps", []):
            with st.expander(f"🔧 {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"):
                st.code(result, language="json")

if question:
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Calling the MCP server…"):
            try:
                reply, steps = asyncio.run(answer(question))
                failed = False
            except Exception as exc:  # noqa: BLE001 — the UI never shows a traceback
                reply, steps, failed = explain_failure(exc), [], True

        st.markdown(reply)
        for name, args, result in steps:
            with st.expander(f"🔧 {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"):
                st.code(result, language="json")
        # Only meaningful when the agent actually answered. After a failure there
        # was no trajectory to have, and saying "no tools were called" reads as a
        # second, invented problem.
        if not steps and not failed:
            st.caption(
                "No tools were called for this answer — worth noticing, since "
                "every question about the business should involve a lookup."
            )

    st.session_state.history.append(
        {"role": "assistant", "content": reply, "steps": steps}
    )
