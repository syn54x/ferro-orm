"""Annotated-style companion to mutations.py (AGENTS.md I-7).

Same ``Customer`` model as docs/examples/mutations.py, declared with
``Annotated`` metadata instead of assignment. Field options move into
``Annotated[...]``; the default stays as the assigned value.
"""

import asyncio

from ferro import UniqueViolationError, connect, engines

# --8<-- [start:models]
from datetime import datetime
from typing import Annotated

from ferro import Field, Model


class Customer(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    email: Annotated[str, Field(unique=True)]
    name: str = ""
    plan: str = "free"
    views: int = 0
    updated_at: datetime | None = None


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        alice = await Customer.create(email="alice@example.com", name="Alice")
        assert (alice.id, alice.plan) == (1, "free")

        try:
            await Customer.create(email="alice@example.com")
        except UniqueViolationError:
            ...  # email is unique in this style too
        else:
            raise AssertionError("expected UniqueViolationError")

    print("mutations_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
