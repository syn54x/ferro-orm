"""Runnable companion to the multiple-databases how-to (docs/pages/howto/multiple-databases.md)."""

import asyncio
from typing import Annotated

from ferro import connect, create_tables, engines, transaction

# --8<-- [start:models]
from ferro import Field, Model


class Metric(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    value: int = 0


# --8<-- [end:models]


async def main() -> None:
    # --8<-- [start:connect]
    await connect("sqlite::memory:", name="app", default=True, auto_migrate=True)
    await connect("sqlite::memory:", name="analytics", auto_migrate=True)

    async with engines.session():
        await Metric.create(name="signups", value=12)  # the default, "app"
        app_metrics = await Metric.all()

    await Metric.using("analytics").create(name="page_views", value=1840)
    analytics_metrics = await Metric.using("analytics").all()

    print(len(app_metrics), len(analytics_metrics))
    # --8<-- [end:connect]
    assert [metric.name for metric in app_metrics] == ["signups"]
    assert [metric.name for metric in analytics_metrics] == ["page_views"]

    # --8<-- [start:session]
    async with engines.session("analytics") as session:
        busy = (
            await session.query(Metric).where(lambda metric: metric.value > 100).all()
        )

    print([metric.name for metric in busy])
    # --8<-- [end:session]
    assert [metric.name for metric in busy] == ["page_views"]

    # --8<-- [start:conflict]
    async with engines.session():
        try:
            await Metric.using("analytics").all()
        except ValueError as error:
            print(error)
        else:
            raise AssertionError("expected ValueError")
    # --8<-- [end:conflict]

    # --8<-- [start:transaction]
    async with transaction(using="analytics"):
        await Metric.create(name="clicks", value=42)  # runs on "analytics"
        try:
            await Metric.using("app").create(name="clicks", value=42)
        except ValueError as error:
            print(error)
        else:
            raise AssertionError("expected ValueError")

    print(len(await Metric.using("analytics").all()))
    # --8<-- [end:transaction]
    async with engines.session():
        assert len(await Metric.all()) == 1

    # --8<-- [start:create-tables]
    await connect("sqlite::memory:", name="archive")
    report = await create_tables(using="archive")

    print([statement.subject for statement in report.statements])
    # --8<-- [end:create-tables]
    assert len(await Metric.using("archive").all()) == 0

    print("multiple_databases example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
