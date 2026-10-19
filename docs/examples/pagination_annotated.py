"""Annotated-style companion to pagination.py (AGENTS.md I-7)."""

import asyncio

# --8<-- [start:model]
from datetime import date
from typing import Annotated

from ferro import Field, Model, connect, engines


class Article(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    title: str
    published_on: Annotated[date, Field(index=True)]


# --8<-- [end:model]

ARTICLES = [
    ("Choosing a primary key", date(2026, 1, 5)),
    ("Indexes that pay for themselves", date(2026, 1, 12)),
    ("Reading a query plan", date(2026, 1, 12)),
    ("Keyset paging in practice", date(2026, 1, 19)),
    ("Soft deletes and unique keys", date(2026, 1, 26)),
    ("Storing time in UTC", date(2026, 2, 2)),
    ("Testing with an in-memory database", date(2026, 2, 9)),
]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await Article.bulk_create(
            [Article(title=title, published_on=day) for title, day in ARTICLES]
        )
        # --8<-- [end:session]

        newest = (
            Article.select()
            .order_by(lambda article: article.published_on, "desc")
            .order_by(lambda article: article.id, "desc")
            .limit(3)
        )
        first_page = await newest.all()
        second_page = await newest.after(first_page[-1]).all()
        assert [article.title for article in second_page] == [
            "Keyset paging in practice",
            "Reading a query plan",
            "Indexes that pay for themselves",
        ]

    print("pagination_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
