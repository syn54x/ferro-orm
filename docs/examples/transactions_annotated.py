"""Annotated-style companion to transactions.py (AGENTS.md I-7).

Same ``Account`` model as docs/examples/transactions.py, declared with
``Annotated`` metadata instead of assignment.
"""

import asyncio

from ferro import IntegrityError, connect, engines, transaction

# --8<-- [start:models]
from typing import Annotated

from ferro import Field, Model


class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    number: Annotated[str, Field(unique=True)]
    owner: str
    balance: int = 0


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        async with transaction():
            await Account.create(number="CHK-001", owner="Alice", balance=100)

        try:
            async with transaction():
                await Account.create(number="CHK-001", owner="Bob")
        except IntegrityError:
            ...  # number is unique in this style too
        else:
            raise AssertionError("expected IntegrityError")

        assert await Account.select().count() == 1

    print("transactions_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
