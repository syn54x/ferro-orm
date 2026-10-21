"""Runnable companion to the Models and fields guide (docs/pages/guide/models-and-fields.md).

A product catalog. ``Product`` is the running example; ``Category``,
``Order`` and ``Review`` join it in the sections that need a model of their
own. Each SQL block on the page is a section of ``DDL`` below, and ``main()``
checks ``DDL`` against the statements ``create_tables()`` ran.
"""

# --8<-- [start:program]
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from ferro import Field, Model, connect, create_tables, engines, varchar


class Status(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class Product(Model):
    # --8<-- [start:composite-uniques]
    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("category", "name"),
    )
    # --8<-- [end:composite-uniques]
    # --8<-- [start:composite-indexes]
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("status", "created_at"),
    )
    # --8<-- [end:composite-indexes]

    # --8<-- [start:primary-key]
    id: int | None = Field(default=None, primary_key=True)
    # --8<-- [end:primary-key]
    sku: str = Field(unique=True)
    # --8<-- [start:db-type]
    name: str = Field(db_type=varchar(200), max_length=200)
    # --8<-- [end:db-type]
    category: str
    # --8<-- [start:validation-fields]
    price: Decimal = Field(ge=0, decimal_places=2)
    # --8<-- [end:validation-fields]
    # --8<-- [start:enum]
    status: Status = Status.DRAFT
    # --8<-- [end:enum]
    # --8<-- [start:json]
    attributes: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # --8<-- [end:json]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


async def main() -> None:
    await connect("sqlite::memory:")
    report = await create_tables()

    async with engines.session():
        lamp = await Product.create(
            sku="LMP-001",
            name="Desk lamp",
            category="lighting",
            price=Decimal("39.90"),
        )
        assert lamp.id == 1  # the database assigned it
        assert lamp.status is Status.DRAFT
        # --8<-- [end:program]
        assert _statements(DDL) == sorted(
            _normalized(statement.sql) for statement in report.statements
        )
        await tour(lamp)

    print("models_and_fields example ran successfully")


import re  # noqa: E402
import uuid  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from ferro import UniqueViolationError, fetch_one  # noqa: E402


# --8<-- [start:category]
class Category(Model):
    # --8<-- [start:table-name]
    __ferro_table__: ClassVar[str] = "categories"
    # --8<-- [end:table-name]

    code: str = Field(primary_key=True)  # a natural key, such as "lighting"
    title: str


# --8<-- [end:category]


# --8<-- [start:order]
class Order(Model):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    total: Decimal


# --8<-- [end:order]


# --8<-- [start:base-class]
class Timestamped(Model):
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Review(Timestamped):
    id: int | None = Field(default=None, primary_key=True)
    product_sku: str
    rating: int


# --8<-- [end:base-class]


async def tour(lamp: Product) -> None:
    # --8<-- [start:save]
    floor_lamp = Product(
        sku="LMP-002",
        name="Floor lamp",
        category="lighting",
        price=Decimal("129.00"),
    )
    assert floor_lamp.id is None
    await floor_lamp.save()
    assert floor_lamp.id == 2
    # --8<-- [end:save]

    # --8<-- [start:json-round-trip]
    shade = await Product.create(
        sku="SHD-001",
        name="Linen shade",
        category="lighting",
        price=Decimal("24.50"),
        attributes={"colour": "oat", "fabric": "linen"},
        tags=["bedroom", "living room"],
    )
    stored = await Product.where(lambda product: product.sku == "SHD-001").first()
    assert stored.attributes == {"colour": "oat", "fabric": "linen"}
    assert stored.tags == ["bedroom", "living room"]
    # --8<-- [end:json-round-trip]
    assert stored is shade

    # --8<-- [start:enum-value]
    lamp.status = Status.ACTIVE
    await lamp.save()
    row = await fetch_one('SELECT "status" FROM "product" WHERE "id" = ?', lamp.id)
    assert row == {"status": "active"}
    # --8<-- [end:enum-value]

    await Category.create(code="lighting", title="Lighting")
    assert (await Category.get("lighting")).title == "Lighting"

    # --8<-- [start:uuid-value]
    order = await Order.create(total=Decimal("68.40"))
    row = await fetch_one('SELECT "id" FROM "order"')
    assert row == {"id": str(order.id)}  # 36 characters, hyphens included
    # --8<-- [end:uuid-value]

    # --8<-- [start:composite-unique-violation]
    try:
        await Product.create(
            sku="LMP-003",
            name="Desk lamp",
            category="lighting",
            price=Decimal("42.00"),
        )
    except UniqueViolationError as exc:
        assert "UNIQUE constraint failed: product.category, product.name" in str(exc)
    # --8<-- [end:composite-unique-violation]
    else:
        raise AssertionError("expected UniqueViolationError")

    # --8<-- [start:validation]
    try:
        await Product.create(
            sku="LMP-004",
            name="Clamp lamp",
            category="lighting",
            price=Decimal("-5.00"),
        )
    except ValidationError as exc:
        [error] = exc.errors()
        assert error["loc"] == ("price",)
        assert error["msg"] == "Input should be greater than or equal to 0"
    # --8<-- [end:validation]
    else:
        raise AssertionError("expected ValidationError")

    # --8<-- [start:base-class-rows]
    await Review.create(product_sku="LMP-001", rating=5)
    assert await Review.select().count() == 1
    assert await Timestamped.select().count() == 0  # a table of its own
    # --8<-- [end:base-class-rows]


DDL = """
# --8<-- [start:product-ddl]
# --8<-- [start:product-table]
CREATE TABLE IF NOT EXISTS "product" (
  "attributes" JSON NOT NULL,
  "category" varchar NOT NULL,
  "created_at" DATETIME NOT NULL,
  "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
# --8<-- [start:name-column]
  "name" varchar(200) NOT NULL,
# --8<-- [end:name-column]
  "price" NUMERIC NOT NULL,
  "sku" varchar NOT NULL,
# --8<-- [start:status-column]
  "status" varchar(7) NOT NULL,
# --8<-- [end:status-column]
  "tags" JSON NOT NULL
);
# --8<-- [end:product-table]
# --8<-- [start:sku-unique]
CREATE UNIQUE INDEX IF NOT EXISTS "uq_product_sku" ON "product" ("sku");
# --8<-- [end:sku-unique]
# --8<-- [start:composite-unique-ddl]
CREATE UNIQUE INDEX IF NOT EXISTS "uq_product_category_name" ON "product" ("category", "name");
# --8<-- [end:composite-unique-ddl]
# --8<-- [start:composite-index-ddl]
CREATE INDEX IF NOT EXISTS "idx_product_status_created_at" ON "product" ("status", "created_at");
# --8<-- [end:composite-index-ddl]
# --8<-- [end:product-ddl]
# --8<-- [start:category-ddl]
CREATE TABLE IF NOT EXISTS "categories" (
  "code" varchar NOT NULL PRIMARY KEY,
  "title" varchar NOT NULL
);
# --8<-- [end:category-ddl]
# --8<-- [start:order-ddl]
CREATE TABLE IF NOT EXISTS "order" (
  "id" CHAR(32) NOT NULL PRIMARY KEY,
  "total" NUMERIC NOT NULL
);
# --8<-- [end:order-ddl]
# --8<-- [start:base-class-ddl]
CREATE TABLE IF NOT EXISTS "timestamped" (
  "created_at" DATETIME NOT NULL
);
CREATE TABLE IF NOT EXISTS "review" (
  "created_at" DATETIME NOT NULL,
  "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
  "product_sku" varchar NOT NULL,
  "rating" integer NOT NULL
);
# --8<-- [end:base-class-ddl]
"""


def _normalized(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip()


def _statements(ddl: str) -> list[str]:
    """The statements of ``ddl``, snippet markers left out, one line each."""
    text = "\n".join(line for line in ddl.splitlines() if "--8<--" not in line)
    return sorted(_normalized(sql) for sql in text.split(";") if sql.strip())


if __name__ == "__main__":
    asyncio.run(main())
