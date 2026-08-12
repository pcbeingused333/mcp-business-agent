"""
Tests for the evaluation harness itself.

An eval that is wrong is worse than no eval: it produces numbers that look
authoritative and are not. The grounding checks in particular went through four
rounds of false positives before they were trustworthy — each one is pinned
below, because every one of them made a correct agent answer look like a
fabrication.
"""
import pytest

from evals import dataset
from evals.score import score_scenario
from evals.trajectory import (
    Trajectory,
    ToolCall,
    args_match,
    counts_in,
    date_components,
    forbidden_used,
    missing_tools,
    money_in,
    order_respected,
    summarise,
    ungrounded_counts,
    ungrounded_money,
)


def traj(answer="", calls=(), results=()) -> Trajectory:
    return Trajectory(
        calls=[ToolCall(n, a) for n, a in calls], results=list(results), answer=answer
    )


# ---- extraction ----

def test_money_is_found_and_normalised():
    assert money_in("that is $1,234.56 total") == {"1234.56"}
    assert money_in("$1234.56") == money_in("$1,234.56")


def test_counts_ignore_money_dates_years_and_percentages():
    text = "On 2026-09-19 the deposit is 25 % of $640.00 for 80 people"
    assert counts_in(text) == {80}


def test_typographic_hyphens_in_dates_do_not_become_quantities():
    """
    Regression. Models write 2026‑09‑19 with U+2011, not an ASCII hyphen, so the
    ISO pattern missed it and 2026, 9 and 19 were reported as invented figures
    across ten of twelve scenarios.
    """
    assert counts_in("booked for 2026‑09‑19") == set()


def test_numbers_followed_by_a_comma_are_still_found():
    """
    Regression. The original pattern used a lookahead rejecting a following
    comma, so `"on_hand": 40,` in a JSON tool result never matched while a bare
    40 in prose did — grounded figures were reported as invented.
    """
    assert 40 in counts_in('{"on_hand": 40, "reorder_level": 25}')


def test_list_markers_are_not_quantities():
    """Regression. Models offer numbered options constantly."""
    assert counts_in("Options:\n1. Raise the headcount\n2. Pick a date\n3. Ask") == set()


def test_date_components_licence_long_form_renderings():
    """
    Regression. "el sábado 19 de septiembre" repeats a date the tool returned;
    stripping ISO dates alone does not cover it, and the check is meant to be
    language-independent.
    """
    assert date_components('{"day": "2026-09-19"}') == {2026, 9, 19}


# ---- grounding ----

def test_a_price_the_tools_never_returned_is_flagged():
    """
    The check that catches a quote produced from memory — the real failure that
    prompted this harness. It stays correct even when the invented figure
    happens to be right, because the question is whether it looked it up.
    """
    t = traj(answer="That comes to $560.00.", results=['{"total": "$640.00 CAD"}'])
    assert ungrounded_money(t, "quote 80 people") == {"560.00"}


def test_a_price_that_came_from_a_tool_is_not_flagged():
    t = traj(answer="That comes to $640.00.", results=['{"total": "$640.00 CAD"}'])
    assert ungrounded_money(t, "quote 80 people") == set()


def test_a_computed_shortfall_is_flagged():
    """The '5 servings short' bug: the tools said 70 and 80, never 5."""
    t = traj(
        answer="You are 5 servings short.",
        results=['{"remaining_servings": 70}'],
    )
    assert 5 in ungrounded_counts(t, "can you do 80 people?")


def test_numbers_the_customer_supplied_are_grounded():
    """The agent may repeat the question back without having invented anything."""
    t = traj(answer="For 80 people, yes.", results=['{"remaining_servings": 250}'])
    assert ungrounded_counts(t, "can you do 80 people?") == set()


def test_arguments_the_agent_sent_count_as_grounded():
    t = traj(
        answer="I checked for 120 people.",
        calls=[("quote_catering", {"headcount": 120})],
        results=['{"feasible": true}'],
    )
    assert ungrounded_counts(t, "quote a big party") == set()


# ---- tool selection, order, arguments ----

def test_missing_and_forbidden_tools_are_detected():
    t = traj(calls=[("place_order", {})])
    assert missing_tools(t, ["quote_catering"]) == {"quote_catering"}
    assert forbidden_used(t, ["place_order"]) == {"place_order"}


def test_order_requires_the_prerequisite_to_come_first():
    good = traj(calls=[("quote_catering", {}), ("place_order", {})])
    bad = traj(calls=[("place_order", {}), ("quote_catering", {})])
    assert order_respected(good, "quote_catering", "place_order")
    assert not order_respected(bad, "quote_catering", "place_order")


def test_order_is_satisfied_when_the_later_tool_was_never_called():
    t = traj(calls=[("quote_catering", {})])
    assert order_respected(t, "quote_catering", "place_order")


def test_argument_checking_reports_the_best_matching_call():
    """
    An agent that explored alternatives is judged on its best attempt.

    Scoring the first call would mark a correct trajectory wrong whenever the
    agent checked another date before settling on the right one.
    """
    t = traj(
        calls=[
            ("quote_catering", {"day": "2026-09-05", "headcount": 80}),
            ("quote_catering", {"day": "2026-09-19", "headcount": 80}),
        ]
    )
    assert args_match(t, "quote_catering", {"day": "2026-09-19", "headcount": 80}) == []


def test_argument_mismatch_names_the_field():
    t = traj(calls=[("quote_catering", {"day": "2026-09-05"})])
    (mismatch,) = args_match(t, "quote_catering", {"day": "2026-09-19"})
    assert "day" in mismatch


def test_a_tool_never_called_is_reported_rather_than_crashing():
    assert args_match(traj(), "quote_catering", {"day": "x"}) == [
        "quote_catering was never called"
    ]


# ---- scoring a scenario ----

def _scenario(**kwargs) -> dataset.Scenario:
    base = dict(id="t", question="q", check_grounding=False, min_tool_calls=0)
    base.update(kwargs)
    return dataset.Scenario(**base)


def test_a_clean_trajectory_scores_full_marks():
    scenario = _scenario(required_tools=["quote_catering"], must_mention=["640.00"])
    result = score_scenario(
        scenario, traj(answer="$640.00 total", calls=[("quote_catering", {})])
    )
    assert result.passed and result.score == 1.0


def test_a_run_that_errored_is_scored_as_a_failure_not_a_pass():
    scenario = _scenario()
    result = score_scenario(
        scenario, Trajectory(calls=[], results=[], answer="", error="RateLimitError")
    )
    assert not result.passed and "error" in result.failures[0]


def test_answering_with_no_tool_call_at_all_fails():
    """
    Every question in the set is about live business data, so a confident answer
    with no lookup is wrong however plausible it reads.
    """
    scenario = _scenario(min_tool_calls=1)
    result = score_scenario(scenario, traj(answer="Sure, $8 per person."))
    assert any("no-lookup" in failure for failure in result.failures)


def test_any_of_tools_accepts_either_route():
    scenario = _scenario(any_of_tools=["check_availability", "quote_catering"])
    result = score_scenario(scenario, traj(calls=[("quote_catering", {})]))
    assert result.passed


def test_every_failing_check_is_reported_not_just_the_first():
    scenario = _scenario(
        required_tools=["check_stock"],
        forbidden_tools=["place_order"],
        must_mention=["closed"],
    )
    result = score_scenario(scenario, traj(answer="all good", calls=[("place_order", {})]))
    assert len(result.failures) == 3


def test_score_is_the_fraction_of_checks_that_passed():
    # Three checks run: the always-on tool-call floor, the required tool, and
    # the forbidden tool. Two of them fail.
    scenario = _scenario(required_tools=["a"], forbidden_tools=["b"])
    result = score_scenario(scenario, traj(calls=[("b", {})]))
    assert result.checks == 3
    assert result.score == pytest.approx(1 / 3)


# ---- the dataset ----

def test_scenario_ids_are_unique():
    ids = [s.id for s in dataset.SCENARIOS]
    assert len(ids) == len(set(ids))


def test_every_scenario_expects_at_least_one_tool_call():
    for scenario in dataset.SCENARIOS:
        assert scenario.min_tool_calls >= 1, scenario.id


def test_write_scenarios_require_a_quote_first():
    """place_order must never be reachable without a preceding quote."""
    for scenario in dataset.SCENARIOS:
        if "place_order" in scenario.required_tools:
            assert ("quote_catering", "place_order") in scenario.order, scenario.id


def test_the_frozen_dates_land_on_the_weekdays_the_scenarios_assume(db):
    """
    Guards the fixtures against the seed window moving.

    If BUSY_SATURDAY stopped being a part-booked Saturday, the capacity scenario
    would silently start testing nothing.
    """
    from datetime import date

    from ops import store

    assert date.fromisoformat(dataset.BUSY_SATURDAY).weekday() == 5
    assert date.fromisoformat(dataset.FREE_SATURDAY).weekday() == 5
    assert date.fromisoformat(dataset.CLOSED_MONDAY).weekday() == 0

    store.seed(db_path=db, today=dataset.EVAL_TODAY, days=60)
    assert store.capacity_for(dataset.CLOSED_MONDAY, db_path=db) is None
    assert store.capacity_for(dataset.BUSY_SATURDAY, db_path=db).remaining == 70
    assert store.capacity_for(dataset.FREE_SATURDAY, db_path=db).remaining == 250


def test_unknown_scenario_id_fails_loudly():
    with pytest.raises(ValueError):
        dataset.scenarios(["does-not-exist"])


def test_summary_counts_failures_by_kind():
    scenario = _scenario(required_tools=["a"])
    results = [
        score_scenario(scenario, traj(calls=[("a", {})])),
        score_scenario(scenario, traj()),
    ]
    summary = summarise(results)
    assert summary["passed"] == 1 and summary["failures_by_kind"]["missing-tool"] == 1
