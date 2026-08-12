"""
The business rules a catering order has to satisfy, and the quote maths.

Pure functions over plain data — no database, no MCP, no clock of their own
(today is always passed in). That makes every rule directly testable and, more
importantly, keeps the *decision* to accept an order in one readable place
rather than spread across tool handlers.

One deliberate design choice runs through this file: a request that fails is
checked against **every** rule and returns all the blockers at once. Returning
only the first one would force the agent into a guess-and-retry loop — fix the
lead time, call again, discover the headcount is too low, call again — which
costs a round trip per problem and reads to the customer like being told the
bad news one piece at a time.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from ops.money import apply_percent, format as format_money

# --- Policy. Values a business owner would change; the logic below should not
# need editing when they do. ---
MIN_CATERING_SERVINGS = 50
LEAD_TIME_DAYS = 3               # "at least 72 hours notice"
DELIVERY_FEE_CENTS = 2500
FREE_DELIVERY_THRESHOLD_CENTS = 40000
DEPOSIT_PERCENT = 25.0
MAX_HORIZON_DAYS = 365


@dataclass(frozen=True)
class QuoteLine:
    sku: str
    name: str
    quantity: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.unit_price_cents * self.quantity

    def as_dict(self) -> Dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unit_price": format_money(self.unit_price_cents),
            "line_total": format_money(self.total_cents),
            "line_total_cents": self.total_cents,
        }


@dataclass
class Quote:
    """A priced, feasibility-checked catering request."""

    day: str
    servings: int
    lines: List[QuoteLine] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    delivery_cents: int = 0

    @property
    def feasible(self) -> bool:
        return not self.blockers

    @property
    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.delivery_cents

    @property
    def deposit_cents(self) -> int:
        return apply_percent(self.total_cents, DEPOSIT_PERCENT)

    def as_dict(self) -> Dict:
        return {
            "day": self.day,
            "servings": self.servings,
            "feasible": self.feasible,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "lines": [line.as_dict() for line in self.lines],
            "subtotal": format_money(self.subtotal_cents),
            "delivery": format_money(self.delivery_cents),
            "total": format_money(self.total_cents),
            "deposit_due": format_money(self.deposit_cents),
            "total_cents": self.total_cents,
            "deposit_cents": self.deposit_cents,
        }


def days_until(day: str, today: date) -> int:
    """Whole days from today to `day`. Negative for dates already past."""
    return (date.fromisoformat(day) - today).days


def check_lead_time(day: str, today: date) -> Optional[str]:
    """Catering needs LEAD_TIME_DAYS notice. Returns a blocker, or None."""
    delta = days_until(day, today)
    if delta < 0:
        return f"{day} is in the past."
    if delta < LEAD_TIME_DAYS:
        return (
            f"Catering needs {LEAD_TIME_DAYS} days notice; {day} is "
            f"{'today' if delta == 0 else f'in {delta} day(s)'}."
        )
    if delta > MAX_HORIZON_DAYS:
        return f"{day} is more than a year out; bookings are not open that far ahead."
    return None


def check_minimum(servings: int) -> Optional[str]:
    if servings < MIN_CATERING_SERVINGS:
        return (
            f"Catering has a {MIN_CATERING_SERVINGS}-serving minimum; "
            f"this request is for {servings}."
        )
    return None


def check_capacity(servings: int, remaining: Optional[int], day: str) -> Optional[str]:
    """
    `remaining` is None when the date is not open for booking at all.

    Closed and fully-booked are different answers to the customer — one is
    "pick another day", the other is "we could do it if you shift the headcount"
    — so they are never collapsed into a single "unavailable".
    """
    if remaining is None:
        return f"{day} is not open for bookings (the business is closed that day)."
    if servings > remaining:
        return f"{day} has {remaining} servings left, short of the {servings} requested."
    return None


def check_stock(needed: Dict[str, int], on_hand: Dict[str, int]) -> List[str]:
    """One blocker per short item, so the caller learns about all of them at once."""
    blockers = []
    for sku, quantity in sorted(needed.items()):
        available = on_hand.get(sku)
        if available is None:
            blockers.append(f"{sku} is not a stocked item.")
        elif available < quantity:
            blockers.append(f"{sku}: {quantity} needed, {available} in stock.")
    return blockers


def delivery_fee(subtotal_cents: int, wants_delivery: bool) -> int:
    """Free above the threshold; the caller decides whether delivery applies at all."""
    if not wants_delivery:
        return 0
    return 0 if subtotal_cents >= FREE_DELIVERY_THRESHOLD_CENTS else DELIVERY_FEE_CENTS


def near_free_delivery(subtotal_cents: int, wants_delivery: bool) -> Optional[str]:
    """
    Tell the customer when a small increase would remove the delivery fee.

    Worth surfacing rather than leaving implicit: it is the single most common
    thing a human would mention at this point in the conversation.
    """
    if not wants_delivery or subtotal_cents >= FREE_DELIVERY_THRESHOLD_CENTS:
        return None
    gap = FREE_DELIVERY_THRESHOLD_CENTS - subtotal_cents
    if gap <= 5000:  # within $50
        return (
            f"{format_money(gap)} more would clear the "
            f"{format_money(FREE_DELIVERY_THRESHOLD_CENTS)} free-delivery threshold."
        )
    return None


def build_quote(
    day: str,
    servings: int,
    lines: List[QuoteLine],
    today: date,
    remaining_capacity: Optional[int],
    on_hand: Dict[str, int],
    wants_delivery: bool = False,
) -> Quote:
    """
    Price a catering request and check it against every rule.

    Blockers are collected, never short-circuited — see the module docstring.
    A quote with blockers is still fully priced, so the customer can see what
    the order would cost if the blocking problem were resolved.
    """
    quote = Quote(day=day, servings=servings, lines=list(lines))

    for blocker in (
        check_lead_time(day, today),
        check_minimum(servings),
        check_capacity(servings, remaining_capacity, day),
    ):
        if blocker:
            quote.blockers.append(blocker)

    needed = {line.sku: line.quantity for line in lines}
    quote.blockers.extend(check_stock(needed, on_hand))

    quote.delivery_cents = delivery_fee(quote.subtotal_cents, wants_delivery)
    hint = near_free_delivery(quote.subtotal_cents, wants_delivery)
    if hint:
        quote.warnings.append(hint)

    return quote
