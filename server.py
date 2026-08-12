"""
MCP server exposing a small business's operations as tools.

Run it:
    python server.py                     # stdio, for Claude Desktop / Cursor / any MCP client
    python server.py --http              # streamable HTTP on :8000/mcp

Why MCP rather than a handful of LangChain tools: everything here is defined
once and consumed by any MCP client — this project's own agent, Claude Desktop,
an IDE — without the tools being rewritten per framework. That reusability is
the whole argument for the protocol, so the server is the artifact and the agent
is just one of its clients.

This module is a thin adapter. Every decision it makes is delegated to `ops`,
which has no MCP import, so the rules are unit-testable without a transport.
"""
import argparse
from datetime import date
from typing import Dict, List, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ops import rules, store
from ops.money import format as format_money

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

server = MCPServer(
    name="business-ops",
    version="0.1.0",
    instructions=(
        "Operations for a food business: catalog and prices, daily booking "
        "capacity, stock levels, catering quotes, and orders.\n\n"
        "Quoting a catering job is a multi-step task: check the day is open and "
        "has room, price the package, confirm stock, and only then place the "
        "order. `quote_catering` does the first three in one call and returns "
        "every reason a request cannot go ahead, not just the first one — read "
        "`blockers` before proposing anything to the customer.\n\n"
        "All money is returned both as a formatted string for humans and as an "
        "integer number of cents (`*_cents`) for arithmetic. Never do maths on "
        "the formatted string. Dates are ISO (YYYY-MM-DD)."
    ),
)


def _today() -> date:
    """Indirection so tests can freeze the clock without patching `date`."""
    return date.today()


def _parse_day(day: str) -> Optional[str]:
    """Return a normalised ISO date, or None when it does not parse."""
    try:
        return date.fromisoformat(day.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


def _bad_day(day: str) -> Dict:
    return {
        "error": f"'{day}' is not a date I can read. Use YYYY-MM-DD, e.g. 2026-09-19.",
    }


@server.tool(
    name="list_catalog",
    description=(
        "List products the business sells, with unit prices. Optionally filter to one "
        "category (churros, drinks, combos, catering). Use this before quoting so the "
        "prices you cite are the real ones rather than remembered."
    ),
    annotations=READ_ONLY,
)
def list_catalog(category: Optional[str] = None) -> Dict:
    products = store.list_products(category=category)
    if not products and category:
        return {
            "error": f"No category named '{category}'.",
            "available_categories": store.categories(),
        }
    return {
        "count": len(products),
        "categories": store.categories(),
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "unit": p.unit,
                "unit_price": format_money(p.unit_price_cents),
                "unit_price_cents": p.unit_price_cents,
            }
            for p in products
        ],
    }


@server.tool(
    name="check_availability",
    description=(
        "Booking capacity for a date: how many servings are already booked and how many "
        "remain. A date the business is closed returns open=false, which is a different "
        "answer from a date that is fully booked — say which one it is."
    ),
    annotations=READ_ONLY,
)
def check_availability(day: str) -> Dict:
    parsed = _parse_day(day)
    if parsed is None:
        return _bad_day(day)

    capacity = store.capacity_for(parsed)
    if capacity is None:
        upcoming = [
            {"day": c.day, "remaining": c.remaining}
            for c in store.open_days()
            if c.day > parsed and c.remaining > 0
        ][:3]
        return {
            "day": parsed,
            "open": False,
            "reason": "The business is closed that day.",
            "next_open_days": upcoming,
        }

    return {
        "day": parsed,
        "open": True,
        "max_servings": capacity.max_servings,
        "booked_servings": capacity.booked_servings,
        "remaining_servings": capacity.remaining,
        "days_notice": rules.days_until(parsed, _today()),
        "meets_lead_time": rules.check_lead_time(parsed, _today()) is None,
    }


@server.tool(
    name="check_stock",
    description=(
        "Current stock for the given SKUs, or for everything when no SKUs are given. "
        "Flags items at or below their reorder level."
    ),
    annotations=READ_ONLY,
)
def check_stock(skus: Optional[List[str]] = None) -> Dict:
    levels = store.stock_levels(skus=skus)
    found = {level.sku for level in levels}
    unknown = [sku for sku in (skus or []) if sku not in found]
    return {
        "items": [
            {
                "sku": level.sku,
                "name": level.name,
                "on_hand": level.on_hand,
                "reorder_level": level.reorder_level,
                "below_reorder": level.below_reorder,
            }
            for level in levels
        ],
        "unknown_skus": unknown,
    }


@server.tool(
    name="quote_catering",
    description=(
        "Price a catering request and check it against every booking rule at once: "
        "lead time, the minimum order, the day's remaining capacity, and stock. "
        "Returns a full price breakdown plus `blockers` — a list of every reason the "
        "request cannot go ahead as asked, empty when it can. The quote is priced even "
        "when blocked, so the customer can see what it would cost if the problem were "
        "fixed. Nothing is reserved; call place_order to actually book."
    ),
    annotations=READ_ONLY,
)
def quote_catering(
    day: str,
    headcount: int,
    package_sku: str = "BAR-2CHOC",
    extras: Optional[List[Dict]] = None,
    delivery: bool = False,
) -> Dict:
    """
    extras: [{"sku": "CHU-GF", "quantity": 20}, ...] — items on top of the package.
    """
    parsed = _parse_day(day)
    if parsed is None:
        return _bad_day(day)
    if headcount <= 0:
        return {"error": "headcount must be a positive number of people."}

    package = store.get_product(package_sku)
    if package is None:
        return {
            "error": f"No package with SKU '{package_sku}'.",
            "catering_packages": [
                {"sku": p.sku, "name": p.name, "price_per_person": format_money(p.unit_price_cents)}
                for p in store.list_products(category="catering")
            ],
        }

    lines = [
        rules.QuoteLine(
            sku=package.sku,
            name=package.name,
            quantity=headcount,
            unit_price_cents=package.unit_price_cents,
        )
    ]
    for extra in extras or []:
        sku = str(extra.get("sku", ""))
        try:
            quantity = int(extra.get("quantity", 0))
        except (TypeError, ValueError):
            return {"error": f"Quantity for '{sku}' must be a whole number."}
        if quantity <= 0:
            continue
        product = store.get_product(sku)
        if product is None:
            return {"error": f"No product with SKU '{sku}'. Call list_catalog for valid SKUs."}
        lines.append(
            rules.QuoteLine(
                sku=product.sku,
                name=product.name,
                quantity=quantity,
                unit_price_cents=product.unit_price_cents,
            )
        )

    capacity = store.capacity_for(parsed)
    on_hand = {level.sku: level.on_hand for level in store.stock_levels()}

    quote = rules.build_quote(
        day=parsed,
        servings=headcount,
        lines=lines,
        today=_today(),
        remaining_capacity=capacity.remaining if capacity else None,
        on_hand=on_hand,
        wants_delivery=delivery,
    )
    return quote.as_dict()


@server.tool(
    name="place_order",
    description=(
        "Book a catering order and consume the day's capacity. Re-checks every rule "
        "before writing, so a stale quote cannot double-book a date. Only call this "
        "once the customer has agreed to a quote with no blockers — it changes the "
        "business's schedule."
    ),
    annotations=WRITES,
)
def place_order(
    customer: str,
    contact: str,
    day: str,
    headcount: int,
    package_sku: str = "BAR-2CHOC",
    extras: Optional[List[Dict]] = None,
    delivery: bool = False,
    notes: str = "",
) -> Dict:
    quote = quote_catering(
        day=day,
        headcount=headcount,
        package_sku=package_sku,
        extras=extras,
        delivery=delivery,
    )
    if "error" in quote:
        return quote
    if not quote["feasible"]:
        return {
            "booked": False,
            "reason": "The request still has blockers; it was not booked.",
            "blockers": quote["blockers"],
        }
    if not customer.strip() or not contact.strip():
        return {"booked": False, "reason": "A customer name and a contact are required."}

    lines = [
        {
            "sku": line["sku"],
            "quantity": line["quantity"],
            "unit_price_cents": line["line_total_cents"] // line["quantity"],
        }
        for line in quote["lines"]
    ]
    try:
        # store.record_order re-checks capacity inside the same transaction that
        # writes the order — the quote above can be seconds stale.
        order_id = store.record_order(
            customer=customer.strip(),
            contact=contact.strip(),
            day=quote["day"],
            servings=headcount,
            lines=lines,
            total_cents=quote["total_cents"],
            deposit_cents=quote["deposit_cents"],
            notes=notes,
        )
    except ValueError as exc:
        return {"booked": False, "reason": str(exc)}

    return {
        "booked": True,
        "order_id": order_id,
        "day": quote["day"],
        "servings": headcount,
        "total": quote["total"],
        "deposit_due": quote["deposit_due"],
        "message": (
            f"Order #{order_id} confirmed for {headcount} on {quote['day']}. "
            f"Deposit of {quote['deposit_due']} due to hold the date."
        ),
    }


@server.tool(
    name="lookup_order",
    description="Retrieve a booked order by its id, with line items and deposit status.",
    annotations=READ_ONLY,
)
def lookup_order(order_id: int) -> Dict:
    order = store.get_order(order_id)
    if order is None:
        return {"found": False, "reason": f"No order with id {order_id}."}
    return {
        "found": True,
        "order_id": order["id"],
        "customer": order["customer"],
        "contact": order["contact"],
        "day": order["day"],
        "servings": order["servings"],
        "status": order["status"],
        "total": format_money(order["total_cents"]),
        "deposit_due": format_money(order["deposit_cents"]),
        "notes": order["notes"],
        "lines": [
            {
                "sku": line["sku"],
                "name": line["name"],
                "quantity": line["quantity"],
                "unit_price": format_money(line["unit_price_cents"]),
            }
            for line in order["lines"]
        ],
    }


@server.resource(
    "ops://policy",
    name="Booking policy",
    description="The catering rules in force: minimum, lead time, delivery, deposit.",
    mime_type="text/markdown",
)
def policy() -> str:
    """Exposed as a resource, not a tool: it is reference text, not an action."""
    return (
        "# Catering policy\n\n"
        f"- Minimum order: **{rules.MIN_CATERING_SERVINGS} servings**\n"
        f"- Notice required: **{rules.LEAD_TIME_DAYS} days**\n"
        f"- Delivery within downtown: **{format_money(rules.DELIVERY_FEE_CENTS)}**, "
        f"free on orders over {format_money(rules.FREE_DELIVERY_THRESHOLD_CENTS)}\n"
        f"- Deposit to hold a date: **{rules.DEPOSIT_PERCENT:.0f}%** of the total\n"
        "- Closed Mondays\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Business operations MCP server.")
    parser.add_argument(
        "--http", action="store_true", help="serve over streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="(re)seed the demo database and exit")
    args = parser.parse_args()

    if args.seed:
        store.seed()
        print(f"Seeded {store.DEFAULT_DB}")
        return

    # A first run against an empty file would otherwise fail on every tool call
    # with a bare "no such table", which is a poor first impression.
    store.initialise()
    if not store.list_products():
        store.seed()

    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
