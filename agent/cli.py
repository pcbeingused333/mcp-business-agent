"""
Command line for the agent.

    python -m agent.cli "quote a catering job for 80 this Saturday"
    python -m agent.cli                 # interactive
    python -m agent.cli --trace "..."   # show every tool call and result

Needs GROQ_API_KEY (free at https://console.groq.com/keys). Reads .env if present.
"""
import argparse
import asyncio
import os
import sys
from typing import List, Optional

from mcp import Client

SUGGESTIONS = [
    "What does a churro bar cost per person?",
    "Can you do catering for 80 people this Saturday?",
    "Quote 120 people for the first Saturday in October, with delivery.",
    "¿Tenéis hueco para 60 personas el próximo sábado?",
]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # optional convenience; the env var works either way


def _print_trace(messages: List) -> None:
    """Show the tool trajectory — what the agent actually did to get its answer."""
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            print(f"  → {call['name']}({call['args']})", file=sys.stderr)
        if message.__class__.__name__ == "ToolMessage":
            body = str(message.content).replace("\n", " ")
            print(
                f"  ← {body[:160]}{'…' if len(body) > 160 else ''}\n",
                file=sys.stderr,
            )


async def run(questions: List[str], trace: bool, model: Optional[str]) -> int:
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is not set. Free key: https://console.groq.com/keys")
        return 2

    from agent.graph import ask, build_agent
    from langchain_core.messages import HumanMessage
    from ops import store
    import server as srv

    store.initialise()
    if not store.list_products():
        store.seed()

    # Client(MCPServer) speaks the protocol over an in-memory transport: real
    # initialize / list_tools / call_tool round trips, no subprocess. Point it at
    # a URL or a stdio transport instead and nothing else here changes.
    async with Client(srv.server) as client:
        agent = await build_agent(client)

        for question in questions:
            print(f"\n\033[1m> {question}\033[0m")
            if trace:
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=question)]}
                )
                _print_trace(result["messages"])
                print(result["messages"][-1].content)
            else:
                try:
                    print(await ask(agent, question))
                except Exception as exc:  # noqa: BLE001 — a CLI shows a message
                    print(f"The agent failed: {type(exc).__name__}: {exc}")
                    return 1
    return 0


async def interactive(trace: bool, model: Optional[str]) -> int:
    print("Business operations agent. Ctrl-C to quit.\nTry:")
    for suggestion in SUGGESTIONS:
        print(f"  · {suggestion}")

    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question in {"exit", "quit"}:
            return 0
        await run([question], trace=trace, model=model)


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Ask the business operations agent.")
    parser.add_argument("question", nargs="*", help="a question; omit for interactive mode")
    parser.add_argument("--trace", action="store_true", help="show each tool call")
    parser.add_argument("--model", help="override the LLM")
    args = parser.parse_args()

    if args.question:
        return asyncio.run(run([" ".join(args.question)], args.trace, args.model))
    return asyncio.run(interactive(args.trace, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
