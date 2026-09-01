"""
The agent: a LangGraph ReAct loop over whatever tools the MCP server advertises.

No business rule is restated here. The agent's job is to sequence tool calls and
report what comes back; the server decides what is allowed. That split is what
keeps the prompt short — the agent does not need to know the catering minimum,
because `quote_catering` will tell it.
"""
import os
from datetime import date
from typing import List, Optional

from langchain_core.tools import BaseTool
from mcp import Client

from agent.bridge import load_mcp_tools

# llama-3.3-70b-versatile emits malformed tool calls often enough on Groq to
# break a tool-using agent on roughly half of all requests — measured at 5/10 in
# the sibling RAG project. This agent has nothing but tools, so that failure
# rate would be total.
DEFAULT_MODEL = "openai/gpt-oss-120b"

# The RAG project pins the same model, so a retirement takes both public demos
# down at once, and the discovery path is somebody opening a dead link. That is
# not hypothetical — llama-3.3-70b-versatile was retired mid-project and the
# sibling project's probe reported it as a 100% failure rate for a while, because
# every request was going to a model that no longer existed.
#
# Ordered by closeness to the shipped model: its own smaller sibling, then another
# family. `groq/compound*` are agentic systems rather than chat models and
# `allam-2-7b` has a 4k context, so neither can stand in for this agent.
FALLBACK_MODELS = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b")

_resolved_model: Optional[str] = None


def available_models(timeout: float = 8.0) -> Optional[set]:
    """The ids Groq is serving, or None when the catalogue cannot be read.

    None is not an empty set: an unreachable catalogue must never be mistaken for
    "every model is gone".
    """
    import json
    import urllib.request

    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    try:
        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return {m["id"] for m in payload.get("data", [])}
    except Exception:  # noqa: BLE001 — fails open, see resolve_model
        return None


def resolve_model(model: Optional[str] = None, recheck: bool = False) -> str:
    """The chat model to run on, checked once per process against the catalogue.

    Fails open: if the catalogue cannot be read, the configured model is returned
    unchanged, because a checker that takes the agent down when the *checker*
    breaks is worse than the risk it guards against. Loud when it substitutes,
    because an agent quietly answering on a different model than the one its
    evaluation numbers describe is the failure this whole project is about.
    """
    import sys

    global _resolved_model

    configured = model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
    if _resolved_model is not None and not recheck:
        return _resolved_model

    catalogue = available_models()
    if catalogue is None or configured in catalogue:
        _resolved_model = configured
        return configured

    for candidate in FALLBACK_MODELS:
        if candidate in catalogue:
            print(
                f"WARNING: `{configured}` is no longer in Groq's catalogue. Falling "
                f"back to `{candidate}`. The trajectory evals were measured on "
                f"`{configured}` and do not describe this model.",
                file=sys.stderr,
            )
            # The warning is for whoever reads the logs; the metric is what an
            # alarm can watch. A fallback nobody is told about is a slower way of
            # not knowing.
            try:
                from ops import telemetry

                telemetry.record_model_substitution(configured, candidate)
            except Exception:  # noqa: BLE001 — telemetry must never break the agent
                pass
            _resolved_model = candidate
            return candidate

    print(
        f"WARNING: `{configured}` is gone and no fallback is available. Continuing "
        "so the failure is the provider's error rather than a silent substitution.",
        file=sys.stderr,
    )
    _resolved_model = configured
    return configured


def build_system_prompt(today: Optional[date] = None) -> str:
    """
    The operating instructions. Deliberately about *process*, not about policy.

    Today's date is injected because the model has no clock: without it,
    "this Saturday" cannot be resolved to the ISO date every tool expects, and
    the model will guess a plausible-looking wrong one.
    """
    today = today or date.today()
    return (
        "You are the operations assistant for a food business. You answer "
        f"questions and take bookings using the tools available to you.\n\n"
        f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\n"
        "RULES:\n"
        "1. Never answer a question about prices, availability, stock or orders "
        "from memory. Call the tool. The tools are the only source of truth, and "
        "the answers change daily.\n"
        "2. Never say you lack the information, cannot help, or that something "
        "is 'not in the system' until you have called at least one tool and it "
        "came back without the answer. Not knowing is a conclusion you may only "
        "reach after looking, never before. If a question could plausibly "
        "concern the business — products, hours, capacity, prices, policies, "
        "delivery, or anything a customer might ask — search first, then answer, "
        "even if you expect to find nothing.\n"
        "3. Resolve relative dates ('this Saturday', 'next week') to YYYY-MM-DD "
        "yourself, using today's date above, before calling a tool. When naming "
        "the day of the week back to the customer, use the `weekday` field the "
        "tool returned — never work it out from the date yourself.\n"
        "4. Report figures exactly as the tools return them, in the currency "
        "given. Never convert, re-estimate or round money yourself. This covers "
        "every number, not just money: do NOT compute shortfalls, differences, "
        "totals or per-person figures in your head. If a day has 70 left and the "
        "customer wants 80, say both numbers and let them subtract — a wrong "
        "arithmetic aside destroys trust in the figures that are correct.\n"
        "5. When a quote comes back with `blockers`, tell the customer every one "
        "of them, not just the first, and say what the order would cost once the "
        "blocker is resolved (the quote is priced even when blocked).\n"
        "6. Before you propose a specific alternative WITH A PRICE — a smaller "
        "headcount, another date, a different package — call the tool again for "
        "that alternative and quote what it returns. Never price an option you "
        "have not looked up. You may suggest an alternative without a price and "
        "offer to check it.\n"
        "7. `place_order` changes the business's schedule. Only call it once the "
        "customer has seen a quote with no blockers and has explicitly agreed to "
        "it. Never book speculatively to 'check' whether booking would work — "
        "quote_catering already answers that without writing anything.\n"
        "8. A tool result beginning with TOOL ERROR means the call failed, not "
        "that the answer is no. Say what went wrong or try a different approach.\n"
        "9. Be concise. Give the answer, then the supporting detail. Reply in the "
        "language the user writes in."
    )


async def build_agent(
    client: Client,
    model: Optional[str] = None,
    today: Optional[date] = None,
    llm=None,
    tools: Optional[List[BaseTool]] = None,
):
    """
    A ReAct agent wired to the MCP server behind `client`.

    Tools are discovered from the server at build time, so this function needs
    no edit when the server grows one.
    """
    from langgraph.prebuilt import create_react_agent

    if tools is None:
        tools = await load_mcp_tools(client)
    if llm is None:
        from langchain_groq import ChatGroq

        llm = ChatGroq(model=resolve_model(model), temperature=0)

    return create_react_agent(llm, tools, prompt=build_system_prompt(today))


_TRANSIENT = (
    "tool_use_failed", "failed to call a function", "rate limit",
    "429", "500", "502", "503", "overloaded", "timeout",
)


def unwrap(exc: BaseException, _depth: int = 0) -> List[BaseException]:
    """
    Every exception inside a failure, including the ones nested out of sight.

    MCP and LangGraph both run tool calls inside anyio task groups, so a
    RateLimitError arrives wrapped in an ExceptionGroup whose own `str()` is
    "unhandled errors in a TaskGroup (1 sub-exception)" — the real message is
    only on the children. Matching on the outer string therefore classified
    every wrapped failure as unknown: retries never fired, and the deployed demo
    reported a plain rate limit as a crash.

    Chained causes are followed too, since a wrapped error is often re-raised
    with the original attached rather than nested.
    """
    found = [exc]
    if _depth > 10:  # cycles are possible via __context__
        return found
    for child in getattr(exc, "exceptions", None) or []:
        found.extend(unwrap(child, _depth + 1))
    for link in (exc.__cause__, exc.__context__):
        if link is not None and link is not exc:
            found.extend(unwrap(link, _depth + 1))
    return found


def describe(exc: BaseException) -> str:
    """The text of a failure and everything nested inside it, lowercased."""
    return " ".join(f"{type(e).__name__}: {e}" for e in unwrap(exc)).lower()


def is_transient(exc: BaseException) -> bool:
    """Whether a failure is worth retrying with the same input."""
    return any(marker in describe(exc) for marker in _TRANSIENT)


async def ask(agent, question: str, attempts: int = 3) -> str:
    """
    Run one question through the agent, retrying transient LLM failures.

    A rejected API key fails immediately rather than burning the retry budget on
    an error no retry can fix.
    """
    from langchain_core.messages import HumanMessage

    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            result = await agent.ainvoke({"messages": [HumanMessage(content=question)]})
            return result["messages"][-1].content
        except Exception as exc:  # noqa: BLE001 — re-raised below
            last = exc
            if not is_transient(exc) or attempt == attempts - 1:
                raise
    raise last  # unreachable; kept for type-checkers
