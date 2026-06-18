"""Annotated-style companion to predicates.py (AGENTS.md I-8)."""

import asyncio

# --8<-- [start:setup]
from typing import Annotated

from ferro import Field, Model, connect


class User(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    age: int
    role: str = "member"
    archived: bool = False
# --8<-- [end:setup]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    await User.bulk_create(
        [
            User(name="alice", age=34, role="admin"),
            User(name="bob", age=19),
        ]
    )

    adults = await User.where(lambda t: t.age >= 18).all()
    assert len(adults) == 2

    print("predicates_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
