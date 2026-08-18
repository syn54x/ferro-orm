"""Runnable companion for table checks (docs/pages/guide/models-and-fields.md)."""

import asyncio
from typing import ClassVar

from ferro import Check, CheckViolationError, Field, Model, connect, engines

# --8<-- [start:models]
class Pair(Model):
    __ferro_checks__: ClassVar[tuple[Check, ...]] = (
        Check(
            "at_most_one_side",
            lambda pair: (pair.left == None) | (pair.right == None),  # noqa: E711
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    left: str | None = None
    right: str | None = None


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await Pair.create(left="only-left")
        await Pair.create(right="only-right")

        # --8<-- [start:violation]
        try:
            await Pair.create(left="both", right="set")
        except CheckViolationError:
            pass  # ck_pair_at_most_one_side rejected the row
        else:
            raise AssertionError("expected CheckViolationError")
        # --8<-- [end:violation]

    print("table_checks example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
