"""
Bridge: MCP tools -> LangChain tools.

## Why this file exists instead of `langchain-mcp-adapters`

That package is the obvious dependency and was the first choice. Version 0.3.2
pins `mcp<2`, so installing it downgrades the SDK from 2.0.0 to 1.29.0 — and
this project's server is written against the 2.0 API (`MCPServer`, which does
not exist in 1.x; it was `FastMCP` there). Taking the adapter would mean
rewriting a working, tested server against an older SDK to satisfy a client-side
convenience wrapper.

The bridge below is the alternative, and it is small because MCP is a good
protocol: a tool listing already carries a name, a description and a JSON Schema,
which is exactly what a LangChain tool needs. `langchain-core` accepts a raw
JSON Schema dict as `args_schema`, so the server's schema passes straight
through — no hand-maintained pydantic mirror of each tool to drift out of sync.

Re-check this when `langchain-mcp-adapters` supports mcp 2.x; at that point the
dependency is the better answer and this file should go.
"""
import json
from typing import Any, Dict, List

from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.types import CallToolResult


def render_result(result: CallToolResult) -> str:
    """
    Turn a tool result into the string the model reads.

    Prefers `structured_content` (the tool's actual return value) and falls back
    to concatenated text blocks. The server wraps a dict return under a "result"
    key; that envelope is unwrapped here so the model sees the shape the tool's
    description promises rather than an extra layer it has to reason about.

    An error result is prefixed rather than raised: the agent should read the
    failure and adapt — try another date, ask for a smaller headcount — not have
    its turn aborted.
    """
    payload: Any = result.structured_content
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]

    if payload is None:
        parts = [getattr(block, "text", "") for block in (result.content or [])]
        text = "\n".join(part for part in parts if part)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)

    if result.is_error:
        return f"TOOL ERROR: {text or 'the tool failed with no detail.'}"
    return text or "(the tool returned nothing)"


def _wrap(client: Client, name: str, description: str, schema: Dict) -> StructuredTool:
    async def call(**kwargs: Any) -> str:
        # Drop keys the model left as None. Optional MCP parameters are absent,
        # not null, and a null trips schema validation on the server side.
        arguments = {k: v for k, v in kwargs.items() if v is not None}
        return render_result(await client.call_tool(name, arguments))

    return StructuredTool.from_function(
        coroutine=call,
        name=name,
        description=description,
        args_schema=schema,
    )


async def load_mcp_tools(client: Client) -> List[StructuredTool]:
    """
    Every tool the connected MCP server advertises, as LangChain tools.

    Discovered at runtime rather than declared here: adding a tool to the server
    makes it available to the agent with no change on this side, which is the
    property that makes the server worth building as a server.
    """
    listing = await client.list_tools()
    return [
        _wrap(
            client,
            name=tool.name,
            description=tool.description or f"The {tool.name} tool.",
            schema=tool.input_schema,
        )
        for tool in listing.tools
    ]
