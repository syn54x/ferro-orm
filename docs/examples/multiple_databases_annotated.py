"""Annotated-style companion to multiple_databases.py (AGENTS.md I-7).

Same ``Metric`` model as docs/examples/multiple_databases.py, declared with
``Annotated`` metadata instead of assignment.
"""

import asyncio

from ferro import connect, engines

# --8<-- [start:models]
from typing import Annotated

from ferro import Field, Model


class Metric(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    value: int = 0


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", name="app", default=True, auto_migrate=True)
    await connect("sqlite::memory:", name="analytics", auto_migrate=True)

    async with engines.session():
        await Metric.create(name="signups", value=12)
    await Metric.using("analytics").create(name="page_views", value=1840)

    assert len(await Metric.using("app").all()) == 1
    assert len(await Metric.using("analytics").all()) == 1

    print("multiple_databases_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
