"""Annotated-style companion to models_and_fields.py (AGENTS.md I-7).

The same models, with each ``Field(...)`` moved inside ``Annotated[...]`` and
the default kept as the assigned value. ``main()`` checks that they create
the same schema, statement for statement, as the assignment-style models.
"""

# --8<-- [start:program]
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar

from ferro import Field, Model, connect, create_tables, engines, varchar


class Status(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class Product(Model):
    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("category", "name"),
    )
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("status", "created_at"),
    )

    # --8<-- [start:primary-key]
    id: Annotated[int | None, Field(primary_key=True)] = None
    # --8<-- [end:primary-key]
    sku: Annotated[str, Field(unique=True)]
    # --8<-- [start:db-type]
    name: Annotated[str, Field(db_type=varchar(200), max_length=200)]
    # --8<-- [end:db-type]
    category: str
    # --8<-- [start:validation-fields]
    price: Annotated[Decimal, Field(ge=0, decimal_places=2)]
    # --8<-- [end:validation-fields]
    status: Status = Status.DRAFT
    # --8<-- [start:json]
    attributes: Annotated[dict[str, str], Field(default_factory=dict)]
    tags: Annotated[list[str], Field(default_factory=list)]
    # --8<-- [end:json]
    created_at: Annotated[datetime, Field(default_factory=lambda: datetime.now(UTC))]


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
        assert _assignment_style_ddl() == sorted(
            _normalized(statement.sql) for statement in report.statements
        )
        await Category.create(code="lighting", title="Lighting")
        order = await Order.create(total=Decimal("68.40"))
        assert (await Order.get(order.id)).total == Decimal("68.40")
        await Review.create(product_sku="LMP-001", rating=5)
        assert await Timestamped.select().count() == 0

    print("models_and_fields_annotated example ran successfully")


import ast  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402


# --8<-- [start:category]
class Category(Model):
    __ferro_table__: ClassVar[str] = "categories"

    code: Annotated[str, Field(primary_key=True)]  # a natural key, such as "lighting"
    title: str


# --8<-- [end:category]


# --8<-- [start:order]
class Order(Model):
    id: Annotated[uuid.UUID, Field(default_factory=uuid.uuid4, primary_key=True)]
    total: Decimal


# --8<-- [end:order]


# --8<-- [start:base-class]
class Timestamped(Model):
    created_at: Annotated[datetime, Field(default_factory=lambda: datetime.now(UTC))]


class Review(Timestamped):
    id: Annotated[int | None, Field(primary_key=True)] = None
    product_sku: str
    rating: int


# --8<-- [end:base-class]


def _normalized(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip()


def _assignment_style_ddl() -> list[str]:
    """The ``DDL`` string of models_and_fields.py, read without importing it
    (importing would register its models beside these)."""
    source = (Path(__file__).parent / "models_and_fields.py").read_text(
        encoding="utf-8"
    )
    [ddl] = [
        node.value.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and [target.id for target in node.targets if isinstance(target, ast.Name)]
        == ["DDL"]
        and isinstance(node.value, ast.Constant)
    ]
    text = "\n".join(line for line in ddl.splitlines() if "--8<--" not in line)
    return sorted(_normalized(sql) for sql in text.split(";") if sql.strip())


if __name__ == "__main__":
    asyncio.run(main())
