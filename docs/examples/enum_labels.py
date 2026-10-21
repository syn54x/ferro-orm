# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for label addition (docs/pages/guide/schema/auto-migrate.md).

``Plan`` after it gained ``PRO``. The page's connect block runs these models
on PostgreSQL against a database whose ``plan`` type has only ``'free'``
(``docs/examples/_preludes/enum_labels.py``). On its own the script saves an
author on SQLite, where an enum is stored as text and has no type to grow.
"""

# --8<-- [start:models]
from enum import StrEnum

from ferro import Field, Model


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"  # new: the live type has only 'free'


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)
    plan: Plan = Plan.FREE


# --8<-- [end:models]

import asyncio

from ferro import connect, engines


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        author = await Author.create(
            name="Ada Lovelace", email="ada@example.com", plan=Plan.PRO
        )
        assert (await Author.get(author.id)).plan is Plan.PRO


if __name__ == "__main__":
    asyncio.run(main())
