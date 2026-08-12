# Business Operations MCP Server

An **MCP server** that exposes a small food business's operations — catalog, booking
capacity, stock, catering quotes, and orders — as tools any MCP client can call.

Built with the [Model Context Protocol](https://modelcontextprotocol.io) Python SDK
(`mcp` 2.0). The tools are defined once and work in Claude Desktop, Cursor, or this
project's own agent, without being rewritten per framework. That reusability is the
argument for the protocol, so **the server is the artifact** and the agent is one of
its clients.

A LangGraph agent ships with it as one client, so you can see the tools drive a real
multi-step conversation.

> Status: server, rules, agent, and tests are done. Trajectory evals and a hosted
> demo are next — see Roadmap.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python server.py --seed        # build the demo database
python server.py               # stdio transport
python server.py --http        # streamable HTTP on :8000/mcp
```

### Use it from Claude Desktop

```json
{
  "mcpServers": {
    "business-ops": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

## The tools

| Tool | Reads / writes | What it does |
|---|---|---|
| `list_catalog` | read | Products and unit prices, filterable by category |
| `check_availability` | read | Remaining booking capacity for a date |
| `check_stock` | read | On-hand quantities, flagging items below reorder level |
| `quote_catering` | read | Prices a request **and** checks every booking rule |
| `place_order` | **write** | Books an order and consumes the day's capacity |
| `lookup_order` | read | Retrieves a booked order |

Plus one resource, `ops://policy` — the booking rules as Markdown. It is reference
text rather than an action, which is exactly the distinction MCP resources exist for.

Read-only tools carry `readOnlyHint`, so a client can auto-approve them and prompt
only for `place_order` — the one call that changes the business's schedule.

## The agent

```bash
cp .env.example .env          # add a free GROQ_API_KEY
python -m agent.cli "Can you do catering for 80 people this Saturday? We'd want delivery."
python -m agent.cli --trace   # same, but print every tool call and result
python -m agent.cli           # interactive
```

A LangGraph ReAct loop (Groq, `openai/gpt-oss-120b`) over whatever tools the server
advertises. It contains **no business rules** — it does not know the catering
minimum, because `quote_catering` tells it. Add a tool to the server and the agent
can use it with no change on the agent side; that property is the reason to build a
server rather than a bag of framework-specific functions.

### Not `langchain-mcp-adapters`

That package is the obvious dependency and was the first choice. Version 0.3.2 pins
`mcp<2`, so installing it downgrades the SDK from 2.0.0 to 1.29.0 — and this server
is written against the 2.0 API (`MCPServer`, which does not exist in 1.x). Taking the
adapter would mean rewriting a working, tested server against an older SDK to satisfy
a client-side convenience wrapper.

`agent/bridge.py` is the alternative, and it is short because MCP is a good protocol:
a tool listing already carries a name, a description and a JSON Schema, and
`langchain-core` accepts a raw JSON Schema as `args_schema`. The server's schema
passes straight through — there is no hand-maintained pydantic mirror of each tool to
drift out of sync. When the adapter supports mcp 2.x, the dependency becomes the
better answer and that file should go.

### Three agent bugs that only appeared when it ran

Unit tests cover the tools. None of these would have failed one.

| Symptom | Fix |
|---|---|
| Asked about 80 people on a day with 70 left, it reported the request was "**5 servings short**". It is 10. | Prompt: report the two numbers, never compute the difference |
| It priced a 70-person alternative **from memory** instead of calling the tool. The figures happened to be right — this time. | Prompt: never price an option you have not looked up; suggest it and offer to check |
| It called Sunday 2026‑08‑16 "**Saturday**" while every figure in the same answer was correct. | Tools: return a `weekday` field, so the model never derives one |

The third is the interesting one, because the fix was not in the prompt. A model that
has to *derive* a value will sometimes derive it wrong, confidently, in the middle of
an otherwise correct answer. Returning `weekday` next to `day` costs nothing and
removes the whole class — the same reasoning as returning `total_cents` next to
`total` so it never does mental arithmetic on money. **Where a tool can hand over a
derived value, that beats instructing the model not to get it wrong.**

Each has a regression test in `tests/test_agent.py`.

## Three decisions worth explaining

### Every blocker is returned at once, not the first one

A catering request can fail four ways simultaneously: too little notice, under the
minimum, over the day's capacity, and short on stock. `quote_catering` checks all of
them and returns the full list.

Short-circuiting on the first failure would cost a round trip per problem — fix the
lead time, call again, discover the headcount is too low, call again — and it reads
to the customer like being told the bad news one piece at a time.

**A blocked quote is still fully priced**, so the customer can see what the order
would cost if the blocker were resolved.

### "Closed" and "fully booked" are different answers

A Monday has no `capacity` row at all; a booked-out Saturday has a row with no
headroom left. The distinction survives from the schema up to the tool response,
because the two send the customer in opposite directions: one means pick another
date, the other means the date works if the headcount changes. Collapsing both into
"unavailable" is the kind of small lie that wastes a customer's afternoon.

A test (`test_seeding_leaves_mondays_absent_rather_than_at_zero_capacity`) pins this,
because a Monday row with `max_servings = 0` would read as "full" to every layer
above it.

### Money is integer cents, everywhere

Every amount is an `int` number of cents; formatting to `$9.50 CAD` happens once, at
the edge. Floats are never used for money — `0.1 + 0.2 != 0.3` in binary floating
point, and a quote that is off by a cent a line is off by real money by the time it
reaches a customer. Responses carry both forms (`total` and `total_cents`) so the
model has something to display and something to compute with.

## Architecture

```
server.py       MCP adapter — tool definitions and argument handling, nothing else
ops/
  store.py      SQLite: catalog, stock, per-day capacity, orders
  rules.py      Booking rules and quote maths — pure functions, no I/O, no clock
  money.py      Integer-cent arithmetic
agent/
  bridge.py     MCP tools -> LangChain tools (see "Not langchain-mcp-adapters")
  graph.py      The ReAct agent and its operating instructions
  cli.py        Ask it things; --trace shows the tool trajectory
tests/          82 tests, no network and no LLM required
```

`ops` has no MCP import and `rules` has no database import. The decision to accept an
order is therefore testable without starting a transport, and the clock is passed in
rather than read, so the suite gives the same answer in January as in July.

The one place that intentionally reaches across the layers is `store.record_order`,
which writes the order and consumes the day's capacity **in a single transaction**.
Two separate writes would let the business double-book a Saturday whenever the second
one failed; `test_a_refused_booking_leaves_no_order_behind` guards it.

## Tests

```bash
pytest -q      # 82 tests, ~1.5s
```

No API key, no network, no running server. Tool tests go through
`server.call_tool(...)` rather than calling the Python functions directly, so the
registered schema and result envelope are covered too — a tool that works when
called directly and fails over the protocol is still broken.

## Roadmap

- An evaluation harness scoring **tool trajectories** — did the agent call the right
  tools, in the right order, with the right arguments — not just the final answer
- A one-click hosted demo

## Licence

MIT.
