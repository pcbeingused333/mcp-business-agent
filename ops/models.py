"""
The domain objects, kept free of any storage concern.

They live apart from the backends so that SQLite and DynamoDB return the same
types, and so the rules and the MCP tools never learn which one is behind them.
"""
from dataclasses import dataclass


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
