"""
Money as integer cents.

Every amount in this project is an `int` number of cents. Floats are not used
for money anywhere: 0.1 + 0.2 != 0.3 in binary floating point, and a catering
quote that is off by a cent per line item is off by real money by the time it
reaches a customer.

Conversion to a human-readable string happens once, at the edge, in `format`.
"""
from typing import Union

CURRENCY = "CAD"


def from_dollars(amount: Union[int, float, str]) -> int:
    """
    Cents from a dollar amount. Only for literals and seed data, never for maths.

    Rounds half away from zero rather than using round(), whose banker's
    rounding turns 2.675 into 2.67 and surprises everyone who checks by hand.
    """
    cents = float(amount) * 100
    return int(cents + (0.5 if cents >= 0 else -0.5))


def format(cents: int, currency: str = CURRENCY) -> str:
    """Render cents for a human: 950 -> '$9.50 CAD'."""
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}${whole}.{remainder:02d} {currency}"


def apply_percent(cents: int, percent: float) -> int:
    """
    A percentage of an amount, rounded to the nearest cent.

    Used for deposits and discounts. Returns an int so the result can keep
    flowing through integer arithmetic instead of leaking a float downstream.
    """
    value = cents * percent / 100
    return int(value + (0.5 if value >= 0 else -0.5))
