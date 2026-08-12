"""
Storage façade: one interface, two backends.

SQLite runs locally, in the tests and in the Streamlit demo. DynamoDB runs in
AWS Lambda, where there is no durable local disk and a shared file would be
wrong across concurrent invocations anyway.

Everything above this line — the rules, the MCP tools, the agent — is written
against the `Store` protocol and never learns which backend is behind it. That
is what makes the AWS deployment a configuration change rather than a rewrite.

Two ways to reach a store:

  * `store.use(...)` sets the process-wide store. The Lambda handler does this
    once per container; the Streamlit app does it once per session.
  * the module-level functions below take an optional `db_path`, which builds a
    throwaway SQLite store for that call. That is the convenience the tests and
    the CLI use, and it is SQLite-only on purpose — a test that wants DynamoDB
    should construct one explicitly rather than have it inferred from a string.
"""
import os
from datetime import date
from typing import Dict, List, Optional, Protocol, runtime_checkable

from ops.backends.sqlite import SqliteStore
from ops.models import DayCapacity, Product, StockLevel

__all__ = [
    "Store", "SqliteStore", "Product", "StockLevel", "DayCapacity",
    "use", "current", "reset", "DEFAULT_DB",
    "initialise", "seed", "list_products", "get_product", "categories",
    "stock_levels", "capacity_for", "open_days", "get_order", "record_order",
    "connect",
]

DEFAULT_DB = os.environ.get(
    "OPS_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops.db"),
)


@runtime_checkable
class Store(Protocol):
    """What every backend must provide. Deliberately small."""

    def initialise(self) -> None: ...
    def seed(self, today: Optional[date] = None, days: int = 30) -> None: ...
    def list_products(self, category: Optional[str] = None) -> List[Product]: ...
    def get_product(self, sku: str) -> Optional[Product]: ...
    def categories(self) -> List[str]: ...
    def stock_levels(self, skus: Optional[List[str]] = None) -> List[StockLevel]: ...
    def capacity_for(self, day: str) -> Optional[DayCapacity]: ...
    def open_days(self) -> List[DayCapacity]: ...
    def get_order(self, order_id: int) -> Optional[Dict]: ...
    def record_order(
        self, customer: str, contact: str, day: str, servings: int,
        lines: List[Dict], total_cents: int, deposit_cents: int, notes: str = "",
    ) -> int: ...


_current: Optional[Store] = None


def use(store: Store) -> Store:
    """Set the process-wide store. Returns it, so callers can chain."""
    global _current
    _current = store
    return store


def reset() -> None:
    """Forget the current store; the next call falls back to DEFAULT_DB."""
    global _current
    _current = None


def current() -> Store:
    """
    The active store, defaulting to SQLite at DEFAULT_DB.

    Read lazily rather than bound at import: DEFAULT_DB is monkeypatched by
    tests and reassigned by the demo, and an import-time snapshot would ignore
    both.
    """
    global _current
    if _current is None:
        _current = SqliteStore(DEFAULT_DB)
    return _current


def _resolve(db_path: Optional[str]) -> Store:
    return SqliteStore(db_path) if db_path else current()


# ---- module-level convenience, delegating to the resolved store ----

def initialise(db_path: Optional[str] = None) -> None:
    _resolve(db_path).initialise()


def seed(db_path: Optional[str] = None, today: Optional[date] = None, days: int = 30) -> None:
    _resolve(db_path).seed(today=today, days=days)


def list_products(category: Optional[str] = None, db_path: Optional[str] = None) -> List[Product]:
    return _resolve(db_path).list_products(category)


def get_product(sku: str, db_path: Optional[str] = None) -> Optional[Product]:
    return _resolve(db_path).get_product(sku)


def categories(db_path: Optional[str] = None) -> List[str]:
    return _resolve(db_path).categories()


def stock_levels(skus: Optional[List[str]] = None, db_path: Optional[str] = None) -> List[StockLevel]:
    return _resolve(db_path).stock_levels(skus)


def capacity_for(day: str, db_path: Optional[str] = None) -> Optional[DayCapacity]:
    return _resolve(db_path).capacity_for(day)


def open_days(db_path: Optional[str] = None) -> List[DayCapacity]:
    return _resolve(db_path).open_days()


def get_order(order_id: int, db_path: Optional[str] = None) -> Optional[Dict]:
    return _resolve(db_path).get_order(order_id)


def record_order(
    customer: str,
    contact: str,
    day: str,
    servings: int,
    lines: List[Dict],
    total_cents: int,
    deposit_cents: int,
    notes: str = "",
    db_path: Optional[str] = None,
) -> int:
    return _resolve(db_path).record_order(
        customer=customer, contact=contact, day=day, servings=servings,
        lines=lines, total_cents=total_cents, deposit_cents=deposit_cents, notes=notes,
    )


def connect(db_path: Optional[str] = None):
    """
    Raw SQLite connection — tests only, and only where the point is the SQL.

    Not part of the `Store` protocol: there is no equivalent on DynamoDB, and
    anything that needs this is asserting on storage internals rather than on
    behaviour.
    """
    store = _resolve(db_path)
    if not isinstance(store, SqliteStore):
        raise TypeError(f"connect() is SQLite-only; the current store is {store!r}")
    return store.connect()
