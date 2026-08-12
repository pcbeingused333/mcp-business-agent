"""
Applying a scenario's expectations to a captured trajectory.

Kept apart from the runner so the whole scoring path is unit-testable against
hand-written trajectories — no model, no network, no database. An eval you
cannot test is an eval you cannot trust, and a scorer that is quietly wrong
produces numbers that look authoritative and are not.
"""
from evals.dataset import Scenario
from evals.trajectory import (
    ScenarioResult,
    Trajectory,
    args_match,
    forbidden_used,
    missing_tools,
    order_respected,
    ungrounded_counts,
    ungrounded_money,
)


def score_scenario(scenario: Scenario, trajectory: Trajectory) -> ScenarioResult:
    """
    Check one trajectory against one scenario.

    Every applicable check is run and every failure recorded — the scorer does
    not stop at the first problem, for the same reason the quoting tool does not:
    one run should tell you everything that is wrong with it.
    """
    failures = []
    checks = 0

    if trajectory.error:
        return ScenarioResult(scenario.id, trajectory, [f"error: {trajectory.error}"], 1)

    checks += 1
    if len(trajectory.calls) < scenario.min_tool_calls:
        failures.append(
            f"no-lookup: answered with {len(trajectory.calls)} tool call(s), "
            f"expected at least {scenario.min_tool_calls}"
        )

    if scenario.required_tools:
        checks += 1
        missing = missing_tools(trajectory, scenario.required_tools)
        if missing:
            failures.append(f"missing-tool: never called {', '.join(sorted(missing))}")

    if scenario.any_of_tools:
        checks += 1
        if not set(scenario.any_of_tools) & set(trajectory.tool_names):
            failures.append(
                f"missing-tool: called none of {', '.join(sorted(scenario.any_of_tools))}"
            )

    if scenario.forbidden_tools:
        checks += 1
        used = forbidden_used(trajectory, scenario.forbidden_tools)
        if used:
            failures.append(f"forbidden-tool: called {', '.join(sorted(used))}")

    for before, after in scenario.order:
        checks += 1
        if not order_respected(trajectory, before, after):
            failures.append(f"order: {after} was called without {before} first")

    for tool, expected in scenario.expected_args.items():
        checks += 1
        mismatches = args_match(trajectory, tool, expected)
        if mismatches:
            failures.append(f"arguments: {'; '.join(mismatches)}")

    for phrase in scenario.must_mention:
        checks += 1
        if phrase.lower() not in trajectory.answer.lower():
            failures.append(f"missing-content: answer never mentions {phrase!r}")

    for phrase in scenario.must_not_mention:
        checks += 1
        if phrase.lower() in trajectory.answer.lower():
            failures.append(f"forbidden-content: answer says {phrase!r}")

    if scenario.check_grounding:
        checks += 1
        stray_money = ungrounded_money(trajectory, scenario.question)
        if stray_money:
            failures.append(
                "ungrounded-money: "
                + ", ".join(f"${m}" for m in sorted(stray_money))
                + " appears in no tool result"
            )

        checks += 1
        stray_counts = ungrounded_counts(trajectory, scenario.question)
        if stray_counts:
            failures.append(
                "ungrounded-count: "
                + ", ".join(str(n) for n in sorted(stray_counts))
                + " appears in no tool result"
            )

    return ScenarioResult(scenario.id, trajectory, failures, checks)
