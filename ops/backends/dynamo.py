"""
DynamoDB backend: the store the Lambda deployment runs on.

## Single-table design

One table, one composite key. Every entity is a partition of related items, so
each read is a GetItem or a Query — never a Scan, which is the cost and latency
trap in DynamoDB.

| Entity    | PK         | SK          | Read pattern                     |
|-----------|------------|-------------|----------------------------------|
| Product   | `PRODUCT`  | `<sku>`     | Query all / GetItem one          |
| Stock     | `STOCK`    | `<sku>`     | Query all / BatchGet a few       |
| Capacity  | `CAPACITY` | `<day>`     | GetItem one date / Query the lot |
| Order     | `ORDER`    | `<id:08d>`  | GetItem                          |
| Counter   | `COUNTER`  | `ORDER`     | Atomic increment                 |

Order ids are zero-padded so the sort key orders numerically as a string.

## The part that is not a translation

`record_order` in SQLite opens a transaction, checks the day's remaining
capacity, inserts the order and decrements capacity. The invariant is that two
concurrent bookings can never push a Saturday past its maximum.

Read-then-write does not hold that invariant here: between the read and the
write another Lambda can book the same day, and both see room. The invariant is
preserved instead by a **conditional write inside `TransactWriteItems`** — the
order Put and the capacity Update commit together or not at all, and the update
carries `remaining >= :servings`, evaluated by DynamoDB at commit time. A losing
writer gets `TransactionCanceledException`, which is translated back into the
same `ValueError` the SQLite backend raises, so the tools above cannot tell the
two apart.

`remaining` is stored alongside `max_servings` and `booked_servings` rather than
derived, precisely so the condition can be a single attribute comparison —
DynamoDB condition expressions cannot do arithmetic, so `booked + n <= max` is
not expressible, while `remaining >= n` is.
"""
import os
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from ops.backends.sqlite import SEED_INVENTORY, SEED_PRODUCTS, seed_capacity
from ops.models import DayCapacity, Product, StockLevel

PRODUCT, STOCK, CAPACITY, ORDER, COUNTER = "PRODUCT", "STOCK", "CAPACITY", "ORDER", "COUNTER"


def _int(value) -> int:
    """DynamoDB returns numbers as Decimal; the domain objects want ints."""
    return int(value) if not isinstance(value, Decimal) else int(value)


class DynamoStore:
    """The `Store` interface, backed by one DynamoDB table."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        resource=None,
        client=None,
        region: Optional[str] = None,
    ):
        import boto3

        region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.table_name = table_name or os.environ.get("OPS_TABLE", "business-ops")
        self._resource = resource or boto3.resource("dynamodb", region_name=region)
        self.table = self._resource.Table(self.table_name)
        # A dedicated low-level client for transact_write_items. Not
        # `resource.meta.client`: that one carries boto3's document interface and
        # re-serialises values, so handing it AttributeValues yields the
        # spectacularly unhelpful "unhashable type: 'dict'" inside a
        # TransactionCanceledException. Keeping the two APIs separate — resource
        # for plain-Python reads, client for typed transactional writes — makes
        # which convention applies obvious at each call site.
        self._client = client or boto3.client("dynamodb", region_name=region)

    def __repr__(self) -> str:
        return f"DynamoStore({self.table_name!r})"

    # ---- schema ----

    def initialise(self) -> None:
        """
        A no-op against a deployed table: Terraform owns the table's existence.

        Creating infrastructure from application code means two sources of truth
        for the same resource, and the one that runs last wins. The table is
        declared in infra/ and this method only verifies it is reachable, so a
        misconfigured table name fails at startup with a clear error rather than
        on the first customer question.
        """
        self.table.load()

    # ---- reads ----

    def _query(self, pk: str) -> List[Dict]:
        from boto3.dynamodb.conditions import Key

        items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk)}
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            token = page.get("LastEvaluatedKey")
            if not token:
                return items
            kwargs["ExclusiveStartKey"] = token

    @staticmethod
    def _product(item: Dict) -> Product:
        return Product(
            sku=item["SK"],
            name=item["name"],
            category=item["category"],
            unit=item["unit"],
            unit_price_cents=_int(item["unit_price_cents"]),
            servings_per_unit=_int(item["servings_per_unit"]),
        )

    def list_products(self, category: Optional[str] = None) -> List[Product]:
        products = [self._product(item) for item in self._query(PRODUCT)]
        if category:
            products = [p for p in products if p.category == category]
        # Sorted here rather than by the sort key: the SQLite backend orders by
        # (category, name) and the two must agree, or the same tool call returns
        # a differently-ordered catalog per deployment.
        return sorted(products, key=lambda p: (p.category, p.name))

    def get_product(self, sku: str) -> Optional[Product]:
        item = self.table.get_item(Key={"PK": PRODUCT, "SK": sku}).get("Item")
        return self._product(item) if item else None

    def categories(self) -> List[str]:
        return sorted({p.category for p in self.list_products()})

    def stock_levels(self, skus: Optional[List[str]] = None) -> List[StockLevel]:
        names = {p.sku: p.name for p in self.list_products()}
        levels = [
            StockLevel(
                sku=item["SK"],
                name=names.get(item["SK"], item["SK"]),
                on_hand=_int(item["on_hand"]),
                reorder_level=_int(item["reorder_level"]),
            )
            for item in self._query(STOCK)
        ]
        if skus:
            wanted = set(skus)
            levels = [level for level in levels if level.sku in wanted]
        return sorted(levels, key=lambda level: level.name)

    @staticmethod
    def _capacity(item: Dict) -> DayCapacity:
        return DayCapacity(
            day=item["SK"],
            max_servings=_int(item["max_servings"]),
            booked_servings=_int(item["booked_servings"]),
        )

    def capacity_for(self, day: str) -> Optional[DayCapacity]:
        item = self.table.get_item(Key={"PK": CAPACITY, "SK": day}).get("Item")
        return self._capacity(item) if item else None

    def open_days(self) -> List[DayCapacity]:
        return sorted(
            (self._capacity(item) for item in self._query(CAPACITY)),
            key=lambda c: c.day,
        )

    def get_order(self, order_id: int) -> Optional[Dict]:
        item = self.table.get_item(Key={"PK": ORDER, "SK": f"{int(order_id):08d}"}).get("Item")
        if not item:
            return None
        names = {p.sku: p.name for p in self.list_products()}
        return {
            "id": _int(item["id"]),
            "customer": item["customer"],
            "contact": item["contact"],
            "day": item["day"],
            "servings": _int(item["servings"]),
            "total_cents": _int(item["total_cents"]),
            "deposit_cents": _int(item["deposit_cents"]),
            "status": item.get("status", "confirmed"),
            "notes": item.get("notes", ""),
            "created_at": item.get("created_at", ""),
            "lines": [
                {
                    "sku": line["sku"],
                    "name": names.get(line["sku"], line["sku"]),
                    "quantity": _int(line["quantity"]),
                    "unit_price_cents": _int(line["unit_price_cents"]),
                }
                for line in item.get("lines", [])
            ],
        }

    # ---- writes ----

    def _next_order_id(self) -> int:
        """
        Atomic counter, the DynamoDB stand-in for AUTOINCREMENT.

        A number handed out here and then lost to a failed transaction leaves a
        gap in the sequence. That is also true of a Postgres sequence after a
        rolled-back insert, and gaps are harmless — the id is an identifier, not
        a count of orders.
        """
        response = self.table.update_item(
            Key={"PK": COUNTER, "SK": ORDER},
            UpdateExpression="ADD seq :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return _int(response["Attributes"]["seq"])

    def record_order(
        self,
        customer: str,
        contact: str,
        day: str,
        servings: int,
        lines: List[Dict],
        total_cents: int,
        deposit_cents: int,
        notes: str = "",
    ) -> int:
        """
        Book an order and consume the day's capacity, atomically.

        See the module docstring: this is a conditional write inside a
        transaction, not a read followed by a write. The condition is what stops
        two concurrent Lambdas from overbooking the same Saturday, and it is
        evaluated by DynamoDB at commit time rather than by us beforehand.
        """
        from botocore.exceptions import ClientError

        capacity = self.capacity_for(day)
        if capacity is None:
            # Cheap pre-check purely for the error message. The condition below
            # is still the authority — this read can be stale.
            raise ValueError(f"{day} is not open for booking")

        order_id = self._next_order_id()
        item = {
            "PK": ORDER,
            "SK": f"{order_id:08d}",
            "id": order_id,
            "customer": customer,
            "contact": contact,
            "day": day,
            "servings": servings,
            "total_cents": total_cents,
            "deposit_cents": deposit_cents,
            "status": "confirmed",
            "notes": notes,
            "lines": [
                {
                    "sku": line["sku"],
                    "quantity": line["quantity"],
                    "unit_price_cents": line["unit_price_cents"],
                }
                for line in lines
            ],
        }

        # transact_write_items lives on the low-level client, which speaks
        # AttributeValue ({"N": "80"}) rather than the plain Python the resource
        # API accepts. TypeSerializer is the supported bridge between the two;
        # handing the client plain values fails at request-validation time.
        from boto3.dynamodb.types import TypeSerializer

        serialize = TypeSerializer().serialize

        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {k: serialize(v) for k, v in item.items()},
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": serialize(CAPACITY),
                                "SK": serialize(day),
                            },
                            "UpdateExpression": (
                                "SET booked_servings = booked_servings + :n, "
                                "remaining = remaining - :n"
                            ),
                            # DynamoDB conditions cannot do arithmetic, so
                            # `booked + n <= max` is not expressible. `remaining`
                            # exists as a stored attribute for exactly this.
                            "ConditionExpression": (
                                "attribute_exists(SK) AND remaining >= :n"
                            ),
                            "ExpressionAttributeValues": {":n": serialize(servings)},
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                # Translated into the same error the SQLite backend raises, so
                # the tools above cannot tell the backends apart.
                fresh = self.capacity_for(day)
                remaining = fresh.remaining if fresh else 0
                raise ValueError(
                    f"{day} has {remaining} servings left; the order needs {servings}"
                ) from exc
            raise
        return order_id

    def seed(self, today: Optional[date] = None, days: int = 30) -> None:
        """Load the demo business. Same data as the SQLite backend, by import."""
        today = today or date.today()
        with self.table.batch_writer() as batch:
            for sku, name, category, unit, price, servings in SEED_PRODUCTS:
                batch.put_item(Item={
                    "PK": PRODUCT, "SK": sku, "name": name, "category": category,
                    "unit": unit, "unit_price_cents": price, "servings_per_unit": servings,
                })
            for sku, on_hand, reorder in SEED_INVENTORY:
                batch.put_item(Item={
                    "PK": STOCK, "SK": sku, "on_hand": on_hand, "reorder_level": reorder,
                })
            for day, max_servings, booked in seed_capacity(today, days):
                batch.put_item(Item={
                    "PK": CAPACITY, "SK": day,
                    "max_servings": max_servings,
                    "booked_servings": booked,
                    "remaining": max_servings - booked,
                })
