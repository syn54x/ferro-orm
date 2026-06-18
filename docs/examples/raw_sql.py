"""Runnable companion to the Raw SQL guide (docs/pages/guide/raw-sql.md)."""

import asyncio

from ferro import Field, Model, connect, execute, fetch_all, fetch_one, transaction


class Event(Model):
    id: int | None = Field(default=None, primary_key=True)
    kind: str
    payload: str = ""


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    await Event.bulk_create(
        [Event(kind="click"), Event(kind="click"), Event(kind="signup")]
    )

    # --8<-- [start:execute]
    affected = await execute("UPDATE event SET payload = ? WHERE kind = ?", "{}", "click")
    # --8<-- [end:execute]
    assert affected == 2

    # --8<-- [start:fetch]
    rows = await fetch_all("SELECT kind, COUNT(*) AS n FROM event GROUP BY kind ORDER BY n DESC")
    top = await fetch_one("SELECT kind FROM event GROUP BY kind ORDER BY COUNT(*) DESC LIMIT 1")
    # --8<-- [end:fetch]
    assert rows[0]["n"] == 2
    assert top is not None and top["kind"] == "click"

    # --8<-- [start:in-transaction]
    async with transaction() as tx:
        await tx.execute("DELETE FROM event WHERE kind = ?", "click")
        remaining = await tx.fetch_all("SELECT * FROM event")
    # --8<-- [end:in-transaction]
    assert len(remaining) == 1

    print("raw_sql example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
