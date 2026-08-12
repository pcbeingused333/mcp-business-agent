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
        "2. Never refuse a question as off-topic without calling a tool first. If "
        "a question could plausibly concern the business — its products, hours, "
        "capacity, prices, policies, or anything a customer might ask — look it "
        "up before concluding anything.\n"
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

        llm = ChatGroq(
            model=model or os.getenv("LLM_MODEL", DEFAULT_MODEL), temperature=0
        )

    return create_react_agent(llm, tools, prompt=build_system_prompt(today))


_TRANSIENT = (
    "tool_use_failed", "failed to call a function", "rate limit",
    "429", "500", "502", "503", "overloaded", "timeout",
)


def is_transient(exc: BaseException) -> bool:
    """Whether a failure is worth retrying with the same input."""
    return any(marker in str(exc).lower() for marker in _TRANSIENT)


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
