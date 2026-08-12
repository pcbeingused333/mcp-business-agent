"""
The DynamoDB backend, against a mocked table (moto) — no AWS account needed.

The two things worth testing here are the ones that are not a translation of the
SQL: the conditional write that preserves the no-overbooking invariant, and the
requirement that both backends answer identically. A backend that is merely
"close enough" produces a tool that behaves differently in production than in
every test.
"""
import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from conftest import FROZEN_TODAY, next_weekday  # noqa: E402
from ops.backends.dynamo import DynamoStore  # noqa: E402
from ops.backends.sqlite import SqliteStore  # noqa: E402
from ops import store as store_module  # noqa: E402

SATURDAY, MONDAY = 5, 0
TABLE = "business-ops-test"


@pytest.fixture
def dynamo():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client = boto3.client("dynamodb", region_name="us-east-1")
        backend = DynamoStore(TABLE, resource=resource, client=client)
        backend.seed(today=FROZEN_TODAY, days=60)
        yield backend


def test_it_satisfies_the_store_protocol(dynamo):
    assert isinstance(dynamo, store_module.Store)


def test_initialise_fails_loudly_on_a_missing_table():
    """
    A wrong table name should break at startup, not on the first customer
    question. Terraform owns the table; this only checks it is reachable.
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        with pytest.raises(Exception):
            DynamoStore("does-not-exist", resource=resource).initialise()


# ---- reads match SQLite ----

@pytest.fixture
def sqlite(tmp_path):
    backend = SqliteStore(str(tmp_path / "cmp.db"))
    backend.seed(today=FROZEN_TODAY, days=60)
    return backend


def test_the_catalog_is_identical_across_backends(dynamo, sqlite):
    """Including order: the same tool call must not return a different list."""
    assert dynamo.list_products() == sqlite.list_products()


def test_category_filtering_matches(dynamo, sqlite):
    assert dynamo.list_products("catering") == sqlite.list_products("catering")


def test_categories_match(dynamo, sqlite):
    assert dynamo.categories() == sqlite.categories()


def test_stock_levels_match(dynamo, sqlite):
    assert dynamo.stock_levels() == sqlite.stock_levels()
    assert dynamo.stock_levels(["CHU-GF"]) == sqlite.stock_levels(["CHU-GF"])


def test_capacity_matches_including_the_closed_day(dynamo, sqlite):
    saturday = next_weekday(SATURDAY, after_days=21)
    monday = next_weekday(MONDAY, after_days=1)
    assert dynamo.capacity_for(saturday) == sqlite.capacity_for(saturday)
    # Closed must stay absent, not zero-capacity, in both backends.
    assert dynamo.capacity_for(monday) is None
    assert sqlite.capacity_for(monday) is None


def test_open_days_match(dynamo, sqlite):
    assert dynamo.open_days() == sqlite.open_days()


def test_an_unknown_product_is_none_in_both(dynamo, sqlite):
    assert dynamo.get_product("NOPE") is None and sqlite.get_product("NOPE") is None


# ---- the conditional write ----

def _book(backend, day, servings, customer="A"):
    return backend.record_order(
        customer=customer, contact="a@example.com", day=day, servings=servings,
        lines=[{"sku": "BAR-2CHOC", "quantity": servings, "unit_price_cents": 800}],
        total_cents=servings * 800, deposit_cents=servings * 200,
    )


def test_a_booking_consumes_capacity(dynamo):
    day = next_weekday(SATURDAY, after_days=21)
    before = dynamo.capacity_for(day).remaining
    _book(dynamo, day, 60)
    assert dynamo.capacity_for(day).remaining == before - 60


def test_a_booking_is_readable_afterwards(dynamo):
    day = next_weekday(SATURDAY, after_days=21)
    order_id = _book(dynamo, day, 60, customer="Acme Ltd")
    order = dynamo.get_order(order_id)
    assert order["customer"] == "Acme Ltd"
    # The line carries the product name, joined from the catalog, as SQLite does.
    assert order["lines"][0]["name"] == "Churro bar, two chocolates"


def test_booking_beyond_capacity_is_refused_with_the_same_error_as_sqlite(dynamo):
    """
    The condition is evaluated by DynamoDB, but the failure must look identical
    to the SQL one — the tools above must not be able to tell the backends apart.
    """
    day = next_weekday(SATURDAY, after_days=21)
    remaining = dynamo.capacity_for(day).remaining
    with pytest.raises(ValueError, match="servings left"):
        _book(dynamo, day, remaining + 1)


def test_a_refused_booking_leaves_no_order_behind(dynamo):
    """
    The whole reason for TransactWriteItems.

    A Put plus a separate conditional Update would leave the order written when
    the capacity check failed.
    """
    day = next_weekday(SATURDAY, after_days=21)
    remaining = dynamo.capacity_for(day).remaining
    with pytest.raises(ValueError):
        _book(dynamo, day, remaining + 500, customer="Ghost")
    assert all(o["SK"] != "GHOST" for o in dynamo._query("ORDER"))
    assert dynamo._query("ORDER") == []


def test_capacity_is_untouched_by_a_refused_booking(dynamo):
    day = next_weekday(SATURDAY, after_days=21)
    before = dynamo.capacity_for(day).remaining
    with pytest.raises(ValueError):
        _book(dynamo, day, before + 1)
    assert dynamo.capacity_for(day).remaining == before


def test_booking_a_closed_day_is_refused(dynamo):
    with pytest.raises(ValueError, match="not open"):
        _book(dynamo, next_weekday(MONDAY, after_days=7), 50)


def test_two_bookings_cannot_oversubscribe_a_day(dynamo):
    """The invariant the SQL transaction held, now held by the condition."""
    day = next_weekday(SATURDAY, after_days=21)
    remaining = dynamo.capacity_for(day).remaining
    _book(dynamo, day, remaining - 10, customer="First")
    with pytest.raises(ValueError):
        _book(dynamo, day, remaining - 10, customer="Second")
    assert dynamo.capacity_for(day).remaining >= 0


def test_order_ids_increase(dynamo):
    day = next_weekday(SATURDAY, after_days=21)
    first = _book(dynamo, day, 50)
    second = _book(dynamo, day, 10)
    assert second > first
