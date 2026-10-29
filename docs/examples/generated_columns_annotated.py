"""Runnable companion for generated columns (docs/pages/guide/models-and-fields.md).

A bank transaction stores its amount in minor units (cents); the database
derives its direction, -1 for money out and 1 for money in. The SQL blocks on
the page are the ``DDL_SQLITE`` and ``DDL_POSTGRES`` sections below, each
checked against the statements ``create_tables()`` ran on its database.
"""

import asyncio
import re
from typing import Annotated, ClassVar

from ferro import Check, Field, Model, connect, create_tables, engines, fn


# --8<-- [start:models]
class Transaction(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    amount_minor: int
    direction: Annotated[
        int,
        Field(
            generated=lambda transaction: fn.sign(transaction.amount_minor), index=True
        ),
    ]


# --8<-- [end:models]


# --8<-- [start:check-model]
class Refund(Model):
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check("within_limit", lambda refund: fn.abs(refund.amount_minor) <= 50000),
    )

    id: Annotated[int | None, Field(primary_key=True)] = None
    amount_minor: int


# --8<-- [end:check-model]

DDL_SQLITE = """
# --8<-- [start:ddl-sqlite]
CREATE TABLE IF NOT EXISTS "transaction" (
  "amount_minor" integer NOT NULL,
  "direction" integer NOT NULL GENERATED ALWAYS AS (sign("amount_minor")) STORED
    CONSTRAINT "_ferro_range_direction" CHECK (typeof("direction") IN ('integer', 'null') AND "direction" BETWEEN -2147483648 AND 2147483647),
  "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS "idx_transaction_direction" ON "transaction" ("direction");
# --8<-- [end:ddl-sqlite]
"""

DDL_POSTGRES = """
# --8<-- [start:ddl-postgres]
CREATE TABLE IF NOT EXISTS "transaction" (
  "amount_minor" integer NOT NULL,
  "direction" integer NOT NULL GENERATED ALWAYS AS (sign(CAST("amount_minor" AS bigint))) STORED,
  "id" serial PRIMARY KEY NOT NULL
);
CREATE INDEX IF NOT EXISTS "idx_transaction_direction" ON "transaction" ("direction");
# --8<-- [end:ddl-postgres]
"""


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _embedded(ddl: str) -> str:
    return "\n".join(line for line in ddl.splitlines() if "--8<--" not in line)


async def main() -> None:
    await connect("sqlite::memory:")
    report = await create_tables()
    created = " ".join(
        f"{statement.sql};"
        for statement in report.statements
        if statement.role == "schema" and statement.subject == "transaction"
    )

    # --8<-- [start:check-ddl]
    for statement in report.statements:
        if statement.role == "schema" and statement.subject == "refund":
            print(statement.sql)
    # --8<-- [end:check-ddl]

    async with engines.session():
        on_postgres = (await Transaction.select().count.sql()).dialect == "postgres"
        expected = _embedded(DDL_POSTGRES if on_postgres else DDL_SQLITE)
        assert _normalized(created) == _normalized(expected), created

        # --8<-- [start:lifecycle]
        transaction = Transaction(amount_minor=-250)
        print(transaction.direction)

        await transaction.save()
        print(transaction.direction)

        transaction.amount_minor = 400
        print(transaction.direction)

        await transaction.save()
        print(transaction.direction)
        # --8<-- [end:lifecycle]

        await Transaction.create(amount_minor=-1200)

        # --8<-- [start:filter]
        spent = await Transaction.where(
            lambda transaction: transaction.direction == -1
        ).all()
        print([transaction.amount_minor for transaction in spent])
        # --8<-- [end:filter]

        # --8<-- [start:refusals]
        try:
            Transaction(amount_minor=-250, direction=-1)
        except TypeError as exc:
            print(f"{type(exc).__name__}: {exc}")

        try:
            transaction.direction = 1
        except TypeError as exc:
            print(f"{type(exc).__name__}: {exc}")
        # --8<-- [end:refusals]

    # --8<-- [start:parity]
    try:

        class Rate(Model):
            id: Annotated[int | None, Field(primary_key=True)] = None
            amount: float
            doubled: Annotated[float, Field(generated=lambda rate: rate.amount * 2)]

    except TypeError as exc:
        print(f"{type(exc).__name__}: {exc}")
    # --8<-- [end:parity]

    print("generated_columns example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
