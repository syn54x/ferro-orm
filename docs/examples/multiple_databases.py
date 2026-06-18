"""Runnable companion to the Multiple Databases how-to (docs/pages/howto/multiple-databases.md)."""

import asyncio

from ferro import Field, Model, connect, transaction


class Metric(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    value: float = 0.0


async def main() -> None:
    # --8<-- [start:connect]
    await connect("sqlite::memory:", name="app", default=True, auto_migrate=True)
    await connect("sqlite::memory:", name="analytics", auto_migrate=True)
    # --8<-- [end:connect]

    # --8<-- [start:routing]
    # Writes go to the default ("app") connection unless routed
    await Metric.create(name="signups", value=1)

    # Route reads and writes to a named connection with .using()
    await Metric.using("analytics").create(name="page_views", value=100)

    app_metrics = await Metric.all()
    analytics_metrics = await Metric.using("analytics").all()
    # --8<-- [end:routing]
    assert len(app_metrics) == 1
    assert len(analytics_metrics) == 1
    assert analytics_metrics[0].name == "page_views"

    # --8<-- [start:transaction]
    async with transaction(using="analytics"):
        await Metric.using("analytics").create(name="clicks", value=42)
    # --8<-- [end:transaction]
    assert len(await Metric.using("analytics").all()) == 2
    assert len(await Metric.all()) == 1

    print("multiple_databases example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
