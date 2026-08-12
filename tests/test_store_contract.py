"""
The storage contract every backend must satisfy.

SQLite runs locally and in tests; DynamoDB runs in Lambda. These tests pin the
interface between them, so a backend that quietly omits a method or returns a
different type fails here rather than in production — where the only symptom
would be a tool erroring on one deployment and not the other.
"""
import inspect

import pytest

from ops import store
from ops.backends.sqlite import SqliteStore
from ops.models import DayCapacity, Product, StockLevel


def test_the_sqlite_backend_satisfies_the_protocol(db):
    assert isinstance(SqliteStore(db), store.Store)


def test_the_protocol_covers_every_operation_the_tools_need():
    """
    A method missing here is a method a second backend can silently skip.

    These are exactly the calls server.py makes; if the tool surface grows, this
    list is where the new backend requirement gets declared.
    """
    required = {
        "initialise", "seed", "list_products", "get_product", "categories",
        "stock_levels", "capacity_for", "open_days", "get_order", "record_order",
    }
    assert required <= {n for n in dir(store.Store) if not n.startswith("_")}


def test_backends_return_domain_objects_not_rows(db):
    """
    The rules layer destructures these, so a backend returning dicts would break
    it in a way no type checker catches at runtime.
    """
    backend = SqliteStore(db)
    assert all(isinstance(p, Product) for p in backend.list_products())
    assert all(isinstance(s, StockLevel) for s in backend.stock_levels())
    assert all(isinstance(c, DayCapacity) for c in backend.open_days())


def test_use_sets_the_process_wide_store(tmp_path):
    backend = SqliteStore(str(tmp_path / "x.db"))
    store.use(backend)
    assert store.current() is backend


def test_reset_falls_back_to_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DEFAULT_DB", str(tmp_path / "fallback.db"))
    store.use(SqliteStore(str(tmp_path / "other.db")))
    store.reset()
    assert store.current().path.endswith("fallback.db")


def test_a_db_path_argument_bypasses_the_current_store(tmp_path, db):
    """
    The SQLite-only convenience the tests and CLI use.

    It must not disturb the process-wide store, or a single call with db_path
    would silently repoint everything after it.
    """
    other = str(tmp_path / "other.db")
    store.seed(db_path=other)
    before = store.current()
    store.list_products(db_path=other)
    assert store.current() is before


def test_connect_refuses_a_non_sqlite_store():
    """Raw SQL access has no DynamoDB equivalent; failing loudly beats pretending."""
    class Fake:
        pass

    store.use(Fake())
    with pytest.raises(TypeError, match="SQLite-only"):
        store.connect()
