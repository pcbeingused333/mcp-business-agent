"""
Tests for the storage layer and the MCP tools as an agent actually reaches them.

The tool tests go through `server.call_tool`, not the plain Python functions, so
the registered schema, argument coercion and result envelope are covered too — a
tool that works when called directly and fails over the protocol is still broken.
"""
import asyncio

import pytest

from conftest import FROZEN_TODAY, next_weekday
from ops import store

SATURDAY = 5
MONDAY = 0


# ---- storage ----

def test_seeding_leaves_mondays_absent_rather_than_at_zero_capacity(db):
    """
    Closed and full must stay distinguishable all the way down to the schema.

    A Monday row with max_servings=0 would read as "fully booked" to every layer
    above it, and the customer would be told to try a smaller party.
    """
    monday = next_weekday(MONDAY, after_days=1)
    assert store.capacity_for(monday, db_path=db) is None


def test_a_booking_consumes_capacity_for_that_day(db):
    day = next_weekday(SATURDAY, after_days=21)
    before = store.capacity_for(day, db_path=db).remaining
    store.record_order(
        customer="A", contact="a@example.com", day=day, servings=60,
        lines=[{"sku": "BAR-2CHOC", "quantity": 60, "unit_price_cents": 800}],
        total_cents=48000, deposit_cents=12000, db_path=db,
    )
    assert store.capacity_for(day, db_path=db).remaining == before - 60


def test_a_booking_larger_than_the_remaining_capacity_is_refused(db):
    day = next_weekday(SATURDAY, after_days=21)
    remaining = store.capacity_for(day, db_path=db).remaining
    with pytest.raises(ValueError, match="servings left"):
        store.record_order(
            customer="A", contact="a@example.com", day=day, servings=remaining + 1,
            lines=[{"sku": "BAR-2CHOC", "quantity": remaining + 1, "unit_price_cents": 800}],
            total_cents=1, deposit_cents=0, db_path=db,
        )


def test_a_refused_booking_leaves_no_order_behind(db):
    """The order write and the capacity update are one transaction, or neither."""
    day = next_weekday(SATURDAY, after_days=21)
    remaining = store.capacity_for(day, db_path=db).remaining
    with pytest.raises(ValueError):
        store.record_order(
            customer="Ghost", contact="g@example.com", day=day, servings=remaining + 500,
            lines=[{"sku": "BAR-2CHOC", "quantity": 1, "unit_price_cents": 800}],
            total_cents=1, deposit_cents=0, db_path=db,
        )
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_booking_a_closed_day_is_refused(db):
    monday = next_weekday(MONDAY, after_days=7)
    with pytest.raises(ValueError, match="not open"):
        store.record_order(
            customer="A", contact="a@example.com", day=monday, servings=50,
            lines=[{"sku": "BAR-2CHOC", "quantity": 50, "unit_price_cents": 800}],
            total_cents=40000, deposit_cents=10000, db_path=db,
        )


# ---- the tool surface ----

def test_every_tool_is_registered_with_a_description(db):
    import server as srv

    tools = asyncio.run(srv.server.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "list_catalog", "check_availability", "check_stock",
        "quote_catering", "place_order", "lookup_order",
    }
    for tool in tools:
        # The description is how the model decides when to call it; an empty one
        # is a silently useless tool.
        assert tool.description and len(tool.description) > 40, tool.name


def test_read_only_tools_are_annotated_as_such(db):
    """
    Lets a client auto-approve reads and gate the write.

    place_order changes the business's schedule; the rest do not.
    """
    import server as srv

    tools = {t.name: t for t in asyncio.run(srv.server.list_tools())}
    assert tools["quote_catering"].annotations.read_only_hint is True
    assert tools["place_order"].annotations.read_only_hint is False


def test_catalog_lists_products_with_prices(tools):
    result = tools("list_catalog", category="catering")
    assert result["count"] >= 1
    assert all("unit_price_cents" in p for p in result["products"])


def test_an_unknown_category_returns_the_valid_ones(tools):
    result = tools("list_catalog", category="sushi")
    assert "error" in result and "catering" in result["available_categories"]


def test_availability_on_a_closed_day_says_closed_and_suggests_alternatives(tools):
    result = tools("check_availability", day=next_weekday(MONDAY, after_days=1))
    assert result["open"] is False
    assert result["next_open_days"], "a closed day should point somewhere useful"


def test_availability_on_an_open_day_reports_the_remaining_headroom(tools):
    result = tools("check_availability", day=next_weekday(SATURDAY, after_days=21))
    assert result["open"] is True
    assert result["remaining_servings"] == result["max_servings"] - result["booked_servings"]


def test_an_unparseable_date_is_rejected_with_the_expected_format(tools):
    result = tools("check_availability", day="next saturday")
    assert "error" in result and "YYYY-MM-DD" in result["error"]


def test_a_clean_catering_request_quotes_as_feasible(tools):
    result = tools("quote_catering", day=next_weekday(SATURDAY, after_days=21), headcount=80)
    assert result["feasible"] is True
    assert result["total_cents"] == 80 * 800
    assert result["deposit_due"] == "$160.00 CAD"


def test_a_request_over_capacity_is_blocked_but_still_priced(tools):
    """The seeded near-term Saturdays are part-booked, so this is a real case."""
    result = tools("quote_catering", day=next_weekday(SATURDAY, after_days=5), headcount=200)
    assert result["feasible"] is False
    assert any("servings left" in b for b in result["blockers"])
    assert result["total_cents"] > 0


def test_a_short_notice_under_minimum_request_reports_both_problems(tools):
    result = tools("quote_catering", day=(FROZEN_TODAY.replace(day=2)).isoformat(), headcount=10)
    assert result["feasible"] is False
    assert len(result["blockers"]) >= 2


def test_extras_are_added_to_the_quote(tools):
    day = next_weekday(SATURDAY, after_days=21)
    plain = tools("quote_catering", day=day, headcount=60)
    with_extras = tools(
        "quote_catering", day=day, headcount=60,
        extras=[{"sku": "CHU-GF", "quantity": 10}],
    )
    assert with_extras["total_cents"] == plain["total_cents"] + 10 * 750
    assert len(with_extras["lines"]) == 2


def test_an_unknown_extra_sku_is_rejected_rather_than_silently_dropped(tools):
    result = tools(
        "quote_catering", day=next_weekday(SATURDAY, after_days=21), headcount=60,
        extras=[{"sku": "NOT-REAL", "quantity": 1}],
    )
    assert "error" in result and "NOT-REAL" in result["error"]


def test_an_unknown_package_returns_the_real_packages(tools):
    result = tools(
        "quote_catering", day=next_weekday(SATURDAY, after_days=21),
        headcount=60, package_sku="BAR-IMAGINARY",
    )
    assert "error" in result
    assert any(p["sku"] == "BAR-2CHOC" for p in result["catering_packages"])


def test_placing_an_order_books_it_and_consumes_capacity(tools, db):
    day = next_weekday(SATURDAY, after_days=21)
    before = store.capacity_for(day, db_path=db).remaining

    result = tools(
        "place_order", customer="Acme Ltd", contact="ops@acme.example",
        day=day, headcount=80,
    )
    assert result["booked"] is True
    assert store.capacity_for(day, db_path=db).remaining == before - 80

    found = tools("lookup_order", order_id=result["order_id"])
    assert found["found"] is True and found["customer"] == "Acme Ltd"


def test_a_blocked_request_is_never_booked(tools, db):
    result = tools(
        "place_order", customer="Acme Ltd", contact="ops@acme.example",
        day=next_weekday(SATURDAY, after_days=5), headcount=200,
    )
    assert result["booked"] is False and result["blockers"]
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_an_order_without_a_contact_is_refused(tools):
    result = tools(
        "place_order", customer="Acme Ltd", contact="   ",
        day=next_weekday(SATURDAY, after_days=21), headcount=80,
    )
    assert result["booked"] is False


def test_two_orders_cannot_oversubscribe_the_same_day(tools, db):
    """
    The race the transaction in record_order exists to prevent.

    Both quotes are generated against the same starting capacity; the second
    booking must be refused rather than pushing the day past its maximum.
    """
    day = next_weekday(SATURDAY, after_days=21)
    remaining = store.capacity_for(day, db_path=db).remaining

    first = tools(
        "place_order", customer="First", contact="a@example.com",
        day=day, headcount=remaining - 10,
    )
    second = tools(
        "place_order", customer="Second", contact="b@example.com",
        day=day, headcount=remaining - 10,
    )
    assert first["booked"] is True
    assert second["booked"] is False
    assert store.capacity_for(day, db_path=db).remaining >= 0


def test_looking_up_a_missing_order_says_so(tools):
    assert tools("lookup_order", order_id=99999)["found"] is False


def test_the_policy_resource_states_the_real_numbers(db):
    import server as srv
    from ops import rules

    contents = list(asyncio.run(srv.server.read_resource("ops://policy")))
    text = "".join(c.content for c in contents)
    assert str(rules.MIN_CATERING_SERVINGS) in text
    assert str(rules.LEAD_TIME_DAYS) in text
