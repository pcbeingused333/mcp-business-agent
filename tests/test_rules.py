"""
Tests for the money arithmetic and the booking rules — no database, no MCP.

These are the decisions that cost real money when they are wrong, so they are
tested directly rather than only through the tools that call them.
"""
from datetime import date, timedelta

import pytest

from ops import money, rules
from ops.rules import QuoteLine

TODAY = date(2026, 9, 1)


def days_out(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


# ---- money ----

def test_dollars_convert_to_whole_cents():
    assert money.from_dollars(6.50) == 650
    assert money.from_dollars(12) == 1200


def test_conversion_rounds_half_away_from_zero_not_to_even():
    # Python's round() gives 2.67 here (banker's rounding), which disagrees with
    # every human who checks the arithmetic by hand.
    assert money.from_dollars(2.675) == 268


def test_a_float_that_cannot_be_represented_still_converts_exactly():
    # 0.1 + 0.2 == 0.30000000000000004; the cents must still be 30.
    assert money.from_dollars(0.1 + 0.2) == 30


def test_format_pads_the_cents():
    assert money.format(950) == "$9.50 CAD"
    assert money.format(1005) == "$10.05 CAD"
    assert money.format(1000) == "$10.00 CAD"


def test_format_handles_a_negative_amount():
    assert money.format(-250) == "-$2.50 CAD"


def test_percentages_return_whole_cents():
    assert money.apply_percent(64000, 25.0) == 16000
    result = money.apply_percent(333, 25.0)
    assert isinstance(result, int) and result == 83


# ---- lead time ----

def test_a_date_inside_the_notice_period_is_blocked():
    assert rules.check_lead_time(days_out(1), TODAY) is not None


def test_a_date_exactly_at_the_notice_boundary_is_allowed():
    assert rules.check_lead_time(days_out(rules.LEAD_TIME_DAYS), TODAY) is None


def test_a_past_date_is_reported_as_past_not_as_short_notice():
    blocker = rules.check_lead_time(days_out(-1), TODAY)
    assert blocker is not None and "past" in blocker


def test_a_date_beyond_the_booking_horizon_is_blocked():
    assert rules.check_lead_time(days_out(400), TODAY) is not None


# ---- minimum and capacity ----

def test_below_the_minimum_is_blocked_and_names_the_number():
    blocker = rules.check_minimum(rules.MIN_CATERING_SERVINGS - 1)
    assert blocker is not None and str(rules.MIN_CATERING_SERVINGS) in blocker


def test_exactly_the_minimum_is_allowed():
    assert rules.check_minimum(rules.MIN_CATERING_SERVINGS) is None


def test_a_closed_day_and_a_full_day_give_different_answers():
    """
    The distinction the customer actually needs.

    "We're closed" means pick another date; "we're full" means the date works if
    the headcount changes. Collapsing both into "unavailable" sends the customer
    down the wrong path.
    """
    closed = rules.check_capacity(50, remaining=None, day="2026-09-07")
    full = rules.check_capacity(50, remaining=10, day="2026-09-07")
    assert closed is not None and "closed" in closed
    assert full is not None and "closed" not in full and "10" in full


def test_capacity_exactly_equal_to_the_request_is_allowed():
    assert rules.check_capacity(70, remaining=70, day="2026-09-07") is None


# ---- stock ----

def test_stock_reports_every_short_item_not_just_the_first():
    blockers = rules.check_stock({"A": 10, "B": 10}, {"A": 1, "B": 2})
    assert len(blockers) == 2


def test_an_unstocked_sku_is_distinguished_from_a_short_one():
    blockers = rules.check_stock({"NOPE": 1}, {"A": 5})
    assert len(blockers) == 1 and "not a stocked item" in blockers[0]


def test_sufficient_stock_produces_no_blockers():
    assert rules.check_stock({"A": 5}, {"A": 5}) == []


# ---- delivery ----

def test_delivery_is_free_at_and_above_the_threshold():
    assert rules.delivery_fee(rules.FREE_DELIVERY_THRESHOLD_CENTS, True) == 0
    assert rules.delivery_fee(rules.FREE_DELIVERY_THRESHOLD_CENTS + 1, True) == 0


def test_delivery_is_charged_below_the_threshold():
    assert rules.delivery_fee(
        rules.FREE_DELIVERY_THRESHOLD_CENTS - 1, True
    ) == rules.DELIVERY_FEE_CENTS


def test_no_delivery_requested_means_no_fee_however_small_the_order():
    assert rules.delivery_fee(100, False) == 0


def test_an_order_just_short_of_free_delivery_gets_a_heads_up():
    hint = rules.near_free_delivery(rules.FREE_DELIVERY_THRESHOLD_CENTS - 2000, True)
    assert hint is not None and "20.00" in hint


def test_no_heads_up_when_the_gap_is_large():
    assert rules.near_free_delivery(1000, True) is None


# ---- the whole quote ----

def _line(qty: int, price: int = 800, sku: str = "BAR-2CHOC") -> QuoteLine:
    return QuoteLine(sku=sku, name="Churro bar", quantity=qty, unit_price_cents=price)


def test_a_clean_request_is_feasible_and_priced():
    quote = rules.build_quote(
        day=days_out(10), servings=80, lines=[_line(80)], today=TODAY,
        remaining_capacity=250, on_hand={"BAR-2CHOC": 500},
    )
    assert quote.feasible
    assert quote.subtotal_cents == 64000
    assert quote.deposit_cents == 16000


def test_every_broken_rule_is_reported_in_one_pass():
    """
    The design decision this project is built around.

    Short notice, under the minimum, over capacity and short on stock — all at
    once. Returning only the first would make the agent fix one problem, call
    again, discover the next, and drip the bad news to the customer.
    """
    quote = rules.build_quote(
        day=days_out(1), servings=10, lines=[_line(10)], today=TODAY,
        remaining_capacity=5, on_hand={"BAR-2CHOC": 2},
    )
    assert not quote.feasible
    assert len(quote.blockers) == 4


def test_a_blocked_quote_is_still_fully_priced():
    """So the customer can see what it would cost if the blocker were resolved."""
    quote = rules.build_quote(
        day=days_out(1), servings=100, lines=[_line(100)], today=TODAY,
        remaining_capacity=0, on_hand={"BAR-2CHOC": 500},
    )
    assert not quote.feasible
    assert quote.total_cents == 80000


def test_delivery_is_added_to_the_total_and_the_deposit():
    quote = rules.build_quote(
        day=days_out(10), servings=50, lines=[_line(50, price=200)], today=TODAY,
        remaining_capacity=250, on_hand={"BAR-2CHOC": 500}, wants_delivery=True,
    )
    assert quote.subtotal_cents == 10000
    assert quote.delivery_cents == rules.DELIVERY_FEE_CENTS
    assert quote.total_cents == 10000 + rules.DELIVERY_FEE_CENTS
    assert quote.deposit_cents == money.apply_percent(quote.total_cents, rules.DEPOSIT_PERCENT)


def test_quote_totals_stay_integers_end_to_end():
    quote = rules.build_quote(
        day=days_out(10), servings=77, lines=[_line(77, price=333)], today=TODAY,
        remaining_capacity=250, on_hand={"BAR-2CHOC": 500},
    )
    for value in (quote.subtotal_cents, quote.total_cents, quote.deposit_cents):
        assert isinstance(value, int)


@pytest.mark.parametrize("headcount", [50, 51, 99, 250])
def test_the_line_total_is_always_price_times_quantity(headcount):
    line = _line(headcount, price=800)
    assert line.total_cents == 800 * headcount
