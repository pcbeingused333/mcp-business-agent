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

**It is live.** The server runs on AWS Lambda behind a public Function URL, so you
can call it without cloning anything:

```bash
curl -s -X POST https://w2f7mj2jcbr3jberiepx2iu2nu0xpgcm.lambda-url.us-east-1.on.aws/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Point an MCP client at that URL and the six tools show up. See
[Deployed on AWS](#deployed-on-aws) for how it gets there.

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

## Trajectory evaluation

```bash
python -m evals.run_eval                          # 12 scenarios
python -m evals.run_eval --only spanish --repeat 6  # measure a flaky one
python -m evals.run_eval --json out.json
```

Answer-level scoring is not enough for an agent. A reply can be right by luck —
priced from memory, correct that time — and wrong in a way no reader notices, like
a confident weekday that does not match the date. Both happened here, and neither
would fail a final-answer check.

So the harness scores the **trajectory**: which tools were called, in what order,
with which arguments, and whether every figure in the answer traces back to something
a tool returned.

| Check | Catches |
|---|---|
| `no-lookup` | Answering a question about live data with zero tool calls |
| `missing-tool` / `any_of_tools` | Skipping a lookup the answer depends on |
| `forbidden-tool` | Writing to the schedule when only asked a question |
| `order` | `place_order` before `quote_catering` |
| `arguments` | Resolving "this Saturday" to the wrong date, wrong headcount |
| `ungrounded-money` | A price no tool ever returned |
| `ungrounded-count` | A computed figure — the "5 servings short" bug |
| `missing-content` / `forbidden-content` | Reporting a closed day as "fully booked" |

**The grounding checks are the interesting ones, and they need no judge model.** Pull
every figure out of the answer; check it appears in a tool result, in the question, or
in the arguments the agent itself sent. Deterministic, free, no drift — and it stays
correct when an invented number happens to be right, because the question is whether
the agent *looked it up*.

### Results

12 scenarios, `openai/gpt-oss-120b`: **11/12 passed, mean score 0.98**.

The one failure was real and worth the whole exercise. Asked *"Are you on Uber Eats or
DoorDash?"* the agent replied **"I don't have that information available in our
system" with zero tool calls** — the same class of bug as the sibling RAG project's
off-topic refusal. The prompt already forbade refusing as *off-topic*; the model did
not read that as covering a claim of ignorance, so the rule now names it directly:
*not knowing is a conclusion you may only reach after looking.*

It is also **intermittent** — the same scenario passed on the previous run. That is
why `--repeat` exists: a single pass measures a sample of one, and flaky agent
behaviour is exactly what gets written down as "works" after one lucky run.

> **Honest gap:** the post-fix rate on that scenario is unmeasured. The Groq free tier
> has a 200k-token daily cap and this session hit it. The fix has a regression test on
> the prompt text; the behavioural confirmation is still owed.

### The scorer was wrong four times before it was right

Worth stating plainly, because an eval that is wrong is worse than no eval — it
produces numbers that look authoritative and are not. Every one of these marked a
*correct* agent answer as a fabrication:

| False positive | Cause |
|---|---|
| `2026`, `9`, `19` reported as invented, in 10 of 12 scenarios | Models write dates with typographic hyphens (`2026‑09‑19`, U+2011), which the ISO pattern missed |
| `40` and `250` reported as invented although the tools returned them | A lookahead rejecting a following comma skipped every number in a JSON result (`"on_hand": 40,`) |
| `19` in the Spanish scenario | "el sábado 19 de septiembre" repeats a date the tool returned; stripping ISO dates does not cover long-form renderings |
| `3` in a numbered list of options | A list marker is presentation, not a quantity |

Each has a regression test in `tests/test_evals.py`. The first draft of this harness
reported **2/12 passing**; almost all of that was the scorer, not the agent.

## Live demo

```bash
streamlit run app.py
```

The agent with its tool trajectory rendered next to every answer — which tools it
reached for, with what arguments, and what came back. A chat window that answers
correctly proves nothing about an agent; the trajectory is the demonstration.

Self-contained on purpose: SQLite, no external database, no embedding model, one API
key. A demo whose database can pause is a demo that is dead exactly when someone
opens it.

**Each visitor gets their own copy of the business.** `place_order` permanently
consumes a day's capacity, so on a single shared database every booking a visitor
makes leaves less room for the next, and after a handful of visits the demo answers
"no availability" to everything and looks broken. A per-session SQLite file in the
temp directory fixes that, and removes any assumption that the deploy's working
directory is writable.

Failures are unwrapped before they are classified. MCP and LangGraph run tool calls
inside anyio task groups, so a rate limit arrives as an `ExceptionGroup` whose own
message says nothing about rate limits — matching on the outer `str(exc)` showed a
plain "try again shortly" as an unexplained crash on the live demo, and stopped the
retry logic from firing at all. Both now walk the whole tree.

Before deploying, the app is booted from a **clean install of `requirements.txt`
alone** and executed headlessly with Streamlit's `AppTest` — "the server starts" is
not the same as "the script runs", and a missing dependency shows up on the first
browser connection, which is exactly when someone is looking.

To deploy on Streamlit Community Cloud, point it at `app.py` and set one secret:

```toml
GROQ_API_KEY = "gsk_..."
```

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
lambda_handler.py  AWS entry point: the same server behind a Function URL
ops/
  store.py      The storage interface and the process-wide active backend
  rules.py      Booking rules and quote maths — pure functions, no I/O, no clock
  money.py      Integer-cent arithmetic
  backends/
    sqlite.py   Local and demo: a file, or one per visitor session
    dynamo.py   Deployed: one single-table design, PK/SK
agent/
  bridge.py     MCP tools -> LangChain tools (see "Not langchain-mcp-adapters")
  graph.py      The ReAct agent and its operating instructions
  cli.py        Ask it things; --trace shows the tool trajectory
evals/
  dataset.py    12 scenarios with the trajectory each should produce
  trajectory.py Scoring — pure functions, no LLM, no network
  score.py      Applying a scenario's expectations to a run
  run_eval.py   The runner
infra/          Terraform for the AWS deployment, and deploy.sh
app.py          Streamlit demo, tool trajectory shown per answer
tests/          157 tests, no network and no LLM required
```

`ops` has no MCP import and `rules` has no database import. The decision to accept an
order is therefore testable without starting a transport, and the clock is passed in
rather than read, so the suite gives the same answer in January as in July.

The one place that intentionally reaches across the layers is `store.record_order`,
which writes the order and consumes the day's capacity **in a single transaction**.
Two separate writes would let the business double-book a Saturday whenever the second
one failed; `test_a_refused_booking_leaves_no_order_behind` guards it.

## Deployed on AWS

The server runs behind a public Lambda Function URL, so it is a **remote MCP
server** — a URL a client connects to, not a repo someone has to clone and run.

```
                 push to main
                      │
              ┌───────▼────────┐
              │ GitHub Actions │  pytest (moto) ─── gate
              │   OIDC, no key │  docker build ── push ── update code ── smoke test
              └───────┬────────┘
                      │ short-lived credentials, refs/heads/main only
   ══════════════════ │ ═══════════════════════ AWS ══════════════════════
                      ▼
   MCP client ──► Function URL ──► Lambda (container, 512 MB)
   (Claude Desktop,   auth NONE      lambda_handler.py
    curl, the agent)                   │  Mangum ── ASGI ── MCP streamable HTTP
                                       │  stateless, JSON responses
                                       ▼
                                  DynamoDB  business-ops
                                  on-demand, PK/SK single table
                                       ▲
                                       │
                                  CloudWatch Logs (14-day retention)

   ECR  mcp-business-agent   ── lifecycle: 3 images, untagged expire in a day
```

`ops.store` is an interface with two backends, so nothing above it changes
between the SQLite demo and the deployed table. `rules.py` does not know either
exists.

### Deploying

```bash
cd infra
./deploy.sh          # build, push, apply, smoke test
./deploy.sh --seed   # ...and repopulate the business (destructive)
```

Two ordering constraints shape that script, and neither is obvious from the
Terraform alone.

A container-image Lambda cannot be created before the image exists, and the
image cannot be pushed before the registry does — so the registry is applied on
its own first. Then the Function URL's hostname is only known after the function
exists, and the function needs that hostname in `ALLOWED_HOSTS` or MCP's DNS
rebinding protection answers every request with 421. A resource cannot depend on
an attribute of something that depends on it, so the final apply feeds this
configuration's own output back in as a variable. Terraform still owns the
value, and there is nothing out-of-band for a later apply to revert.

`ALLOWED_HOSTS` unset means the transport rejects everything rather than
accepting any Host, so a half-finished deploy fails closed.

### What only the real deploy could catch

The image was run under the AWS Runtime Interface Emulator against the real
table before any of this — which the 157 tests against moto cannot do. It proved
the image starts, the handler works under the actual runtime, and boto3 reaches
DynamoDB.

It still was not enough. `CreateFunction` failed with *"The image manifest,
config or layer media type for the source image ... is not supported"*, which
reads like a base image problem and is not one. Recent BuildKit attaches
provenance and SBOM attestations by default, and to carry them it pushes an OCI
image index rather than a plain manifest; Lambda accepts only a single Docker v2
manifest. `--provenance=false --sbom=false` fixes it. Every pre-deploy check
passes while it is broken, because the fault is in what the registry stores, not
in the image.

### CI/CD

Push to `main` runs the suite, builds, pushes, updates the function and smoke
tests it. Authentication is OIDC — GitHub presents a signed token naming the
repository and ref, AWS returns short-lived credentials, and there is no access
key in repository secrets.

The trust policy names `refs/heads/main` rather than `repo:owner/name:*`. The
wildcard also matches `pull_request` runs, and a pull request can come from a
fork, so the broad form lets anyone on GitHub open a PR that assumes the deploy
role.

The role also **cannot run `terraform apply`**. Applying this configuration
creates IAM roles, and a role that can create roles can grant itself anything —
not a capability to hand a public repository's OIDC trust. It can push an image
and repoint the function; infrastructure changes are applied from a workstation.

The smoke test sends three requests, not one, for the same reason the test suite
does: Mangum re-runs the ASGI lifespan on every invocation and MCP's session
manager refuses to start twice, so that failure only appears from the second
request into a warm container.

### Cost

Free tier covers it: a million Lambda requests a month, 25 GB of DynamoDB. The
only real exposure is ECR storage beyond the first 500 MB and CloudWatch logs
kept forever, so the lifecycle policy holds three images and log retention is 14
days. A $5 budget alarm is the backstop, and the Function URL is unauthenticated
by design — the seeded business is fictional and disclosable, but the
invocations are still billable.

## Tests

```bash
pytest -q      # 157 tests, ~6s
```

No API key, no network, no running server. Tool tests go through
`server.call_tool(...)` rather than calling the Python functions directly, so the
registered schema and result envelope are covered too — a tool that works when
called directly and fails over the protocol is still broken.

## Roadmap

- Confirm the no-lookup fix behaviourally (blocked on the Groq daily token cap)
- More scenarios around multi-turn bookings, where the agent has to carry a quote
  across turns before writing

## Licence

MIT.
