"""
Tests for the MCP-to-LangChain bridge and the agent's operating instructions.

No API key and no network: the bridge is exercised against the real server over
an in-memory MCP transport, and the prompt is checked as a string. What needs a
live model — whether the agent actually follows these rules — belongs in the
trajectory evals, not here.
"""
import asyncio
from datetime import date

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from agent import graph
from agent.bridge import load_mcp_tools, render_result
from conftest import FROZEN_TODAY, next_weekday

SATURDAY = 5
MONDAY = 0


# ---- rendering a tool result for the model ----

def _result(structured=None, text=None, is_error=False) -> CallToolResult:
    content = [TextContent(type="text", text=text)] if text else []
    return CallToolResult(content=content, structured_content=structured, is_error=is_error)


def test_the_result_envelope_is_unwrapped():
    """
    The SDK wraps a dict return under "result". The model should see the shape
    the tool's description promised, not an extra layer to reason about.
    """
    rendered = render_result(_result(structured={"result": {"day": "2026-09-05"}}))
    assert '"day"' in rendered and '"result"' not in rendered


def test_a_dict_that_genuinely_has_one_result_key_is_not_over_unwrapped():
    # Only unwrap when "result" is the *sole* key — otherwise a tool legitimately
    # returning {"result": ..., "count": ...} would lose its other fields.
    rendered = render_result(_result(structured={"result": 1, "count": 2}))
    assert '"count"' in rendered


def test_text_content_is_used_when_there_is_no_structured_payload():
    assert "plain answer" in render_result(_result(text="plain answer"))


def test_an_error_result_is_labelled_rather_than_raised():
    """The agent should read the failure and adapt, not have its turn aborted."""
    rendered = render_result(_result(structured={"reason": "nope"}, is_error=True))
    assert rendered.startswith("TOOL ERROR")


def test_an_empty_result_still_says_something():
    assert render_result(_result()) == "(the tool returned nothing)"


def test_unicode_survives_rendering():
    rendered = render_result(_result(structured={"name": "Churrería Calderón"}))
    assert "Churrería" in rendered


# ---- the bridge ----

def _tools(db):
    import server as srv

    async def load():
        async with Client(srv.server) as client:
            return {t.name: t for t in await load_mcp_tools(client)}

    return asyncio.run(load())


def test_every_server_tool_is_bridged(db):
    assert set(_tools(db)) == {
        "list_catalog", "check_availability", "check_stock",
        "quote_catering", "place_order", "lookup_order",
    }


def test_the_servers_json_schema_passes_through_untouched(db):
    """
    The reason this bridge is 60 lines rather than a pydantic mirror per tool.

    langchain-core accepts a raw JSON Schema dict, so there is no second copy of
    each signature to drift out of sync with the server.
    """
    schema = _tools(db)["quote_catering"].args_schema
    assert isinstance(schema, dict)
    assert {"day", "headcount"} <= set(schema["properties"])


def test_descriptions_survive_the_bridge(db):
    """They are how the model decides when to call each tool."""
    for name, tool in _tools(db).items():
        assert tool.description and len(tool.description) > 40, name


def test_a_bridged_tool_actually_calls_the_server(db):
    import server as srv

    async def run():
        async with Client(srv.server) as client:
            tools = {t.name: t for t in await load_mcp_tools(client)}
            return await tools["check_availability"].ainvoke(
                {"day": next_weekday(MONDAY, after_days=1)}
            )

    assert '"open": false' in asyncio.run(run())


def test_optional_arguments_left_as_none_are_dropped(db):
    """
    A model that fills an optional parameter with null must not break the call.

    MCP optional parameters are absent, not null, and a null trips schema
    validation server-side.
    """
    import server as srv

    async def run():
        async with Client(srv.server) as client:
            tools = {t.name: t for t in await load_mcp_tools(client)}
            return await tools["list_catalog"].ainvoke({"category": None})

    assert '"products"' in asyncio.run(run())


# ---- the operating instructions ----

def test_the_prompt_states_todays_date_and_weekday():
    """Without a date the model cannot resolve 'this Saturday' and will guess."""
    prompt = graph.build_system_prompt(date(2026, 9, 1))
    assert "2026-09-01" in prompt and "Tuesday" in prompt


def test_the_agent_is_told_not_to_claim_ignorance_before_calling_a_tool():
    """
    Regression, caught by the trajectory eval rather than by reading traces.

    Asked "Are you on Uber Eats?" the agent replied "I don't have that
    information available in our system" with zero tool calls. The earlier
    wording only forbade refusing as *off-topic*, which the model did not read
    as covering a claim of ignorance — so the rule now names that directly.
    """
    prompt = graph.build_system_prompt().lower()
    assert "never say you lack the information" in prompt
    assert "only reach after looking" in prompt


def test_the_agent_is_told_not_to_do_arithmetic():
    """
    Regression, found by running it: asked about 80 people on a day with 70 left,
    the agent said the request was "5 servings short". It is 10.
    """
    prompt = graph.build_system_prompt().lower()
    assert "shortfall" in prompt or "do not compute" in prompt


def test_the_agent_is_told_to_quote_alternatives_before_pricing_them():
    """
    Regression, found by running it: it priced a 70-person alternative from
    memory instead of calling the tool. The numbers happened to be right.
    """
    prompt = graph.build_system_prompt().lower()
    assert "never price an option you have not looked up" in prompt


def test_the_agent_is_told_to_read_the_weekday_from_the_tool():
    """
    Regression, found by running it: it called Sunday 2026-08-16 a Saturday while
    every figure in the same answer was correct. The tools now return `weekday`.
    """
    assert "`weekday` field" in graph.build_system_prompt()


def test_the_agent_is_told_to_report_all_blockers():
    assert "every one" in graph.build_system_prompt()


def test_the_write_tool_needs_explicit_agreement():
    assert "place_order" in graph.build_system_prompt()


# ---- retry classification ----

@pytest.mark.parametrize(
    "message",
    ["tool_use_failed", "Rate limit reached", "HTTP 503", "Request timeout"],
)
def test_transient_failures_are_recognised(message):
    assert graph.is_transient(Exception(message))


@pytest.mark.parametrize("message", ["invalid api key", "authentication_error"])
def test_permanent_failures_are_not_retried(message):
    """Retrying a rejected key three times just burns the budget."""
    assert not graph.is_transient(Exception(message))


def test_the_default_model_is_the_one_that_survives_tool_calls():
    """
    llama-3.3-70b-versatile failed 5/10 tool calls in the sibling project. This
    agent is nothing but tools, so that rate would be total.
    """
    assert graph.DEFAULT_MODEL == "openai/gpt-oss-120b"


# ---- the weekday field the agent depends on ----

def test_date_carrying_tools_return_the_weekday(tools):
    saturday = next_weekday(SATURDAY, after_days=21)
    assert tools("check_availability", day=saturday)["weekday"] == "Saturday"
    assert tools("quote_catering", day=saturday, headcount=80)["weekday"] == "Saturday"


def test_suggested_alternative_days_carry_their_weekday_too(tools):
    result = tools("check_availability", day=next_weekday(MONDAY, after_days=1))
    assert all("weekday" in day for day in result["next_open_days"])
