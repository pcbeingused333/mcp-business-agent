"""
Run the trajectory evaluation.

    python -m evals.run_eval                  # all scenarios
    python -m evals.run_eval --only spanish   # one or more by id
    python -m evals.run_eval --json out.json  # keep the raw trajectories

Needs GROQ_API_KEY — one agent run per scenario. The scoring itself is
deterministic and has no model in it, so a rerun only varies by what the agent
does, not by how it is judged.

The database is rebuilt in a temporary file seeded from EVAL_TODAY, and the
agent is given the same date, so "this Saturday" resolves to a date the
scenarios can assert on and yesterday's real calendar cannot break the run.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from typing import List, Optional

from mcp import Client

from evals.dataset import EVAL_TODAY, Scenario, scenarios
from evals.score import score_scenario
from evals.trajectory import ScenarioResult, ToolCall, Trajectory, summarise


def capture(messages) -> Trajectory:
    """Pull the tool calls, tool results and final answer out of a LangGraph run."""
    calls: List[ToolCall] = []
    results: List[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls.append(ToolCall(name=call["name"], args=dict(call.get("args") or {})))
        if message.__class__.__name__ == "ToolMessage":
            results.append(str(message.content))
    answer = messages[-1].content if messages else ""
    return Trajectory(calls=calls, results=results, answer=str(answer))


async def run_one(agent, scenario: Scenario, attempts: int = 4) -> ScenarioResult:
    """
    Run one scenario, retrying transient failures.

    Groq's free tier caps tokens per minute, and a scenario that dies on a 429
    would otherwise be scored as an agent failure — the eval would report a
    capability problem where there is an infrastructure one. Retries back off,
    and permanent failures (a rejected key) surface immediately.
    """
    from agent.graph import is_transient
    from langchain_core.messages import HumanMessage

    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            state = await agent.ainvoke(
                {"messages": [HumanMessage(content=scenario.question)]}
            )
            return score_scenario(scenario, capture(state["messages"]))
        except Exception as exc:  # noqa: BLE001 — a crashed run is a result
            last = exc
            if not is_transient(exc) or attempt == attempts - 1:
                break
            await asyncio.sleep(2 ** attempt * 5)

    trajectory = Trajectory(
        calls=[], results=[], answer="", error=f"{type(last).__name__}: {last}"
    )
    return score_scenario(scenario, trajectory)


async def run(only: Optional[List[str]], json_path: Optional[str], model: Optional[str],
              repeat: int = 1) -> int:
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is not set. Free key: https://console.groq.com/keys")
        return 2

    from agent.graph import build_agent
    from ops import store

    selected = scenarios(only)

    with tempfile.TemporaryDirectory() as tmp:
        # A throwaway database per run: place_order writes, and an eval that
        # books real capacity would score differently the second time.
        store.DEFAULT_DB = os.path.join(tmp, "eval.db")
        store.seed(today=EVAL_TODAY, days=60)

        import server as srv

        srv._today = lambda: EVAL_TODAY

        print(f"Frozen date: {EVAL_TODAY.isoformat()} ({EVAL_TODAY.strftime('%A')})")
        print(f"Scenarios: {len(selected)}" + (f" x{repeat} runs" if repeat > 1 else "") + "\n")

        async with Client(srv.server) as client:
            agent = await build_agent(client, model=model, today=EVAL_TODAY)
            results = []
            # Agent behaviour is not deterministic — one scenario here passed on
            # one run and failed on the next with the same prompt. A single pass
            # measures a sample of one; --repeat makes flakiness visible instead
            # of leaving it to whichever run gets written down.
            queue = [s for s in selected for _ in range(repeat)]
            for index, scenario in enumerate(queue, start=1):
                result = await run_one(agent, scenario)
                results.append(result)
                mark = "pass" if result.passed else "FAIL"
                print(
                    f"[{index}/{len(queue)}] {mark}  {scenario.id} "
                    f"({result.score:.0%})  tools: {', '.join(result.trajectory.tool_names) or '—'}"
                )
                for failure in result.failures:
                    print(f"        {failure}")

    summary = summarise(results)
    print(f"\nPassed {summary['passed']}/{summary['scenarios']} "
          f"({summary['pass_rate']:.0%}), mean score {summary['mean_score']:.2f}")
    if summary["failures_by_kind"]:
        print("\n| Failure kind | Count |")
        print("|---|---:|")
        for kind, count in summary["failures_by_kind"].items():
            print(f"| {kind} | {count} |")

    if json_path:
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "summary": summary,
                    "results": [
                        {
                            "id": r.scenario_id,
                            "passed": r.passed,
                            "score": r.score,
                            "failures": r.failures,
                            "tools": r.trajectory.tool_names,
                            "answer": r.trajectory.answer,
                        }
                        for r in results
                    ],
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
        print(f"\nWrote {json_path}")

    return 0 if summary["passed"] == summary["scenarios"] else 1


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Trajectory evaluation for the agent.")
    parser.add_argument("--only", nargs="*", help="scenario ids to run")
    parser.add_argument("--json", metavar="PATH", help="write raw results as JSON")
    parser.add_argument("--model", help="override the LLM under test")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each scenario N times to measure flaky behaviour")
    args = parser.parse_args()
    return asyncio.run(run(args.only, args.json, args.model, args.repeat))


if __name__ == "__main__":
    sys.exit(main())
