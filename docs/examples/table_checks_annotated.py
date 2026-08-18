"""Annotated-style companion to table_checks.py (AGENTS.md I-7)."""

import asyncio
from typing import Annotated, ClassVar

from ferro import Check, CheckViolationError, Field, Model, connect, engines

# --8<-- [start:models]
class Pair(Model):
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check(
            "at_most_one_side",
            lambda pair: (pair.left == None) | (pair.right == None),  # noqa: E711
        ),
    )

    id: Annotated[int | None, Field(default=None, primary_key=True)]
    left: str | None = None
    right: str | None = None


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await Pair.create(left="only-left")

        try:
            await Pair.create(left="both", right="set")
        except CheckViolationError:
            pass
        else:
            raise AssertionError("expected CheckViolationError")

    print("table_checks_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
