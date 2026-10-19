"""Annotated-style companion to concepts.py (AGENTS.md I-7).

Declares the Concepts pages' ``User`` with field options inside
``Annotated[...]``, adds the same four users and checks that the
architecture page's query and the identity map page's guarantee hold for
these models too.
"""

import asyncio

# --8<-- [start:models]
from typing import Annotated

from ferro import Field, Model


class User(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    username: Annotated[str, Field(unique=True)]
    email: str
    age: int
    active: bool = True


# --8<-- [end:models]

from ferro import connect, engines  # noqa: E402

USERS = [
    {"username": "ada", "email": "ada@example.com", "age": 36},
    {"username": "grace", "email": "grace@example.com", "age": 45},
    {"username": "alan", "email": "alan@example.com", "age": 17},
    {"username": "edsger", "email": "edsger@example.com", "age": 52, "active": False},
]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await User.bulk_create([User(**row) for row in USERS])

    async with engines.session():
        users = await (
            User.where(lambda user: user.active == True)
            .order_by(lambda user: user.age, "desc")
            .limit(10)
            .all()
        )
        assert [user.username for user in users] == ["grace", "ada", "alan"]

        ada = await User.where(lambda user: user.username == "ada").first()
        assert await User.get(1) is ada
    print("concepts_annotated: ok")


if __name__ == "__main__":
    asyncio.run(main())
