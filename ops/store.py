"""
SQLite storage for the business: catalog, stock, daily capacity, and orders.

SQLite because the point of this project is the agent and the protocol, not the
database — but the schema is real (foreign keys on, integer cents, a capacity
row per day) so the constraints the agent runs into are the constraints a real
booking system has.

Connections are opened per call and closed. The MCP server can be driven by
several concurrent requests, and a single shared connection would need locking
for no benefit at this size.
"""
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterator, List, Optional

from ops.money import from_dollars

DEFAULT_DB = os.environ.get(
    "OPS_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    unit             TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    servings_per_unit INTEGER NOT NULL DEFAULT 1 CHECK (servings_per_unit > 0)
);

CREATE TABLE IF NOT EXISTS inventory (
    sku           TEXT PRIMARY KEY REFERENCES products(sku),
    on_hand       INTEGER NOT NULL CHECK (on_hand >= 0),
    reorder_level INTEGER NOT NULL DEFAULT 0
);

-- One row per date the business can be booked. A missing row means "not yet
-- opened for booking", which is deliberately different from "fully booked":
-- the agent should say so rather than silently reporting no availability.
CREATE TABLE IF NOT EXISTS capacity (
    day             TEXT PRIMARY KEY,
    max_servings    INTEGER NOT NULL CHECK (max_servings >= 0),
    booked_servings INTEGER NOT NULL DEFAULT 0 CHECK (booked_servings >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer      TEXT NOT NULL,
    contact       TEXT NOT NULL,
    day           TEXT NOT NULL,
    servings      INTEGER NOT NULL CHECK (servings > 0),
    total_cents   INTEGER NOT NULL CHECK (total_cents >= 0),
    deposit_cents INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id         INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku              TEXT NOT NULL REFERENCES products(sku),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_orders_day ON orders(day);
"""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    unit: str
    unit_price_cents: int
    servings_per_unit: int


@dataclass(frozen=True)
class StockLevel:
    sku: str
    name: str
    on_hand: int
    reorder_level: int

    @property
    def below_reorder(self) -> bool:
        return self.on_hand <= self.reorder_level


@dataclass(frozen=True)
class DayCapacity:
    day: str
    max_servings: int
    booked_servings: int

    @property
    def remaining(self) -> int:
        return max(0, self.max_servings - self.booked_servings)


@contextmanager
def connect(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """A connection with foreign keys enforced and rows accessible by name."""
    conn = sqlite3.connect(db_path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite; without it the REFERENCES clauses above are
    # documentation rather than constraints.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialise(db_path: Optional[str] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# ---- reads ----


def list_products(category: Optional[str] = None, db_path: Optional[str] = None) -> List[Product]:
    sql = "SELECT * FROM products"
    args: List = []
    if category:
        sql += " WHERE category = ?"
        args.append(category)
    sql += " ORDER BY category, name"
    with connect(db_path) as conn:
        return [Product(**dict(row)) for row in conn.execute(sql, args)]


def get_product(sku: str, db_path: Optional[str] = None) -> Optional[Product]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    return Product(**dict(row)) if row else None


def categories(db_path: Optional[str] = None) -> List[str]:
    with connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT category FROM products ORDER BY 1")]


def stock_levels(skus: Optional[List[str]] = None, db_path: Optional[str] = None) -> List[StockLevel]:
    sql = (
        "SELECT i.sku, p.name, i.on_hand, i.reorder_level "
        "FROM inventory i JOIN products p ON p.sku = i.sku"
    )
    args: List = []
    if skus:
        sql += f" WHERE i.sku IN ({','.join('?' * len(skus))})"
        args.extend(skus)
    sql += " ORDER BY p.name"
    with connect(db_path) as conn:
        return [StockLevel(**dict(row)) for row in conn.execute(sql, args)]


def capacity_for(day: str, db_path: Optional[str] = None) -> Optional[DayCapacity]:
    """Capacity for a date, or None when the date is not open for booking."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM capacity WHERE day = ?", (day,)).fetchone()
    return DayCapacity(**dict(row)) if row else None


def open_days(db_path: Optional[str] = None) -> List[DayCapacity]:
    with connect(db_path) as conn:
        return [DayCapacity(**dict(r)) for r in conn.execute("SELECT * FROM capacity ORDER BY day")]


def get_order(order_id: int, db_path: Optional[str] = None) -> Optional[Dict]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        lines = conn.execute(
            "SELECT l.sku, p.name, l.quantity, l.unit_price_cents "
            "FROM order_lines l JOIN products p ON p.sku = l.sku WHERE l.order_id = ?",
            (order_id,),
        ).fetchall()
    order = dict(row)
    order["lines"] = [dict(line) for line in lines]
    return order


# ---- writes ----


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
    """
    Persist an order and consume the day's capacity in one transaction.

    Booking and capacity move together deliberately: if the capacity update
    fails, the order must not exist. Two separate writes would let the business
    double-book a Saturday whenever the second one errored.
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM capacity WHERE day = ?", (day,)).fetchone()
        if row is None:
            raise ValueError(f"{day} is not open for booking")
        remaining = row["max_servings"] - row["booked_servings"]
        if servings > remaining:
            raise ValueError(
                f"{day} has {remaining} servings left; the order needs {servings}"
            )

        cursor = conn.execute(
            "INSERT INTO orders (customer, contact, day, servings, total_cents, "
            "deposit_cents, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (customer, contact, day, servings, total_cents, deposit_cents, notes),
        )
        order_id = int(cursor.lastrowid)
        for line in lines:
            conn.execute(
                "INSERT INTO order_lines (order_id, sku, quantity, unit_price_cents) "
                "VALUES (?, ?, ?, ?)",
                (order_id, line["sku"], line["quantity"], line["unit_price_cents"]),
            )
        conn.execute(
            "UPDATE capacity SET booked_servings = booked_servings + ? WHERE day = ?",
            (servings, day),
        )
    return order_id


# ---- seed ----

SEED_PRODUCTS = [
    # sku, name, category, unit, price, servings per unit
    ("CHU-6", "Churros (6 pieces)", "churros", "portion", from_dollars(6.50), 1),
    ("POR-4", "Porras (4 pieces)", "churros", "portion", from_dollars(6.00), 1),
    ("CHU-GF", "Gluten-free churros (6)", "churros", "portion", from_dollars(7.50), 1),
    ("CHU-FILL", "Filled churro", "churros", "each", from_dollars(2.00), 1),
    ("CHOC-S", "Spanish hot chocolate (small)", "drinks", "cup", from_dollars(4.00), 1),
    ("CHOC-L", "Spanish hot chocolate (large)", "drinks", "cup", from_dollars(5.50), 1),
    ("COMBO", "Churros + chocolate combo", "combos", "portion", from_dollars(9.50), 1),
    ("CAFE", "Café con leche", "drinks", "cup", from_dollars(3.50), 1),
    ("ESP", "Espresso", "drinks", "cup", from_dollars(3.00), 1),
    ("BAR-2CHOC", "Churro bar, two chocolates", "catering", "person", from_dollars(8.00), 1),
    ("BAR-DELUXE", "Churro bar, deluxe", "catering", "person", from_dollars(12.00), 1),
]

SEED_INVENTORY = [
    ("CHU-6", 400, 120), ("POR-4", 180, 60), ("CHU-GF", 40, 25),
    ("CHU-FILL", 260, 80), ("CHOC-S", 300, 100), ("CHOC-L", 220, 80),
    ("COMBO", 250, 90), ("CAFE", 500, 150), ("ESP", 500, 150),
    ("BAR-2CHOC", 600, 200), ("BAR-DELUXE", 150, 60),
]


def seed(db_path: Optional[str] = None, today: Optional[date] = None, days: int = 30) -> None:
    """
    Load the demo business: catalog, stock, and a month of bookable days.

    Capacity is not uniform — Mondays are closed (no row at all, so the agent
    must distinguish "closed" from "full"), weekends are larger, and a couple of
    days are pre-booked so that "is Saturday free?" has a real answer.
    """
    initialise(db_path)
    today = today or date.today()

    with connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO products (sku, name, category, unit, unit_price_cents, "
            "servings_per_unit) VALUES (?, ?, ?, ?, ?, ?)",
            SEED_PRODUCTS,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO inventory (sku, on_hand, reorder_level) VALUES (?, ?, ?)",
            SEED_INVENTORY,
        )
        for offset in range(days):
            day = today + timedelta(days=offset)
            weekday = day.weekday()  # Monday == 0
            if weekday == 0:
                continue  # closed Mondays — deliberately absent, not zero
            max_servings = 250 if weekday >= 5 else 150
            # Two nearby Saturdays already have bookings, so availability
            # questions have a non-trivial answer out of the box.
            booked = 180 if (weekday == 5 and offset < 14) else 0
            conn.execute(
                "INSERT OR REPLACE INTO capacity (day, max_servings, booked_servings) "
                "VALUES (?, ?, ?)",
                (day.isoformat(), max_servings, booked),
            )
