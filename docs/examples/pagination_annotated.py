"""Runnable companion to the pagination how-to (docs/pages/howto/pagination.md).

Seven articles, newest first, paged three at a time: by page number with
``offset()``, by position with ``after()`` and ``before()``, and through
``article_page()``, the function the page's FastAPI endpoint calls.
"""

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


# --8<-- [start:offset]
async def get_page(page: int, per_page: int) -> list[Article]:
    return (
        await Article.select()
        .order_by(lambda article: article.published_on, "desc")
        .order_by(lambda article: article.id, "desc")
        .limit(per_page)
        .offset((page - 1) * per_page)
        .all()
    )


# --8<-- [end:offset]


# --8<-- [start:cursor]
NEWEST_FIRST = (
    Article.select()
    .order_by(lambda article: article.published_on, "desc")
    .order_by(lambda article: article.id, "desc")
)


def encode_cursor(article: Article) -> str:
    published_on, article_id = NEWEST_FIRST.position_of(article)
    return f"{published_on.isoformat()}_{article_id}"


def decode_cursor(cursor: str) -> tuple[date, int]:
    published_on, article_id = cursor.split("_")
    return date.fromisoformat(published_on), int(article_id)


async def article_page(cursor: str | None = None, limit: int = 20) -> dict:
    query = NEWEST_FIRST.limit(limit + 1)
    if cursor is not None:
        query = query.after(decode_cursor(cursor))
    rows = await query.all()
    items = rows[:limit]
    return {
        "items": items,
        "next_cursor": encode_cursor(items[-1]) if len(rows) > limit else None,
    }


# --8<-- [end:cursor]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await Article.bulk_create(
            [
                Article(title="Choosing a primary key", published_on=date(2026, 1, 5)),
                Article(title="Indexes that pay off", published_on=date(2026, 1, 12)),
                Article(title="Reading a query plan", published_on=date(2026, 1, 12)),
                Article(title="Keyset paging", published_on=date(2026, 1, 19)),
                Article(title="Soft deletes", published_on=date(2026, 1, 26)),
                Article(title="Storing UTC", published_on=date(2026, 2, 2)),
                Article(title="Testing with SQLite", published_on=date(2026, 2, 9)),
            ]
        )
        # --8<-- [end:session]

        # --8<-- [start:offset-call]
        page_two = await get_page(page=2, per_page=3)
        assert [article.title for article in page_two] == [
            "Keyset paging",
            "Reading a query plan",
            "Indexes that pay off",
        ]
        # --8<-- [end:offset-call]

        # --8<-- [start:after]
        newest = (
            Article.select()
            .order_by(lambda article: article.published_on, "desc")
            .order_by(lambda article: article.id, "desc")
            .limit(3)
        )

        first_page = await newest.all()
        assert [article.title for article in first_page] == [
            "Testing with SQLite",
            "Storing UTC",
            "Soft deletes",
        ]

        second_page = await newest.after(first_page[-1]).all()
        assert [article.title for article in second_page] == [
            "Keyset paging",
            "Reading a query plan",
            "Indexes that pay off",
        ]
        # --8<-- [end:after]

        # --8<-- [start:before]
        previous_page = await newest.before(second_page[0]).all()
        assert previous_page == first_page
        # --8<-- [end:before]

        # --8<-- [start:cursor-calls]
        first = await article_page(limit=3)
        assert first["next_cursor"] == "2026-01-26_5"

        second = await article_page(cursor=first["next_cursor"], limit=3)
        assert second["next_cursor"] == "2026-01-12_2"

        last = await article_page(cursor=second["next_cursor"], limit=3)
        assert [article.title for article in last["items"]] == [
            "Choosing a primary key"
        ]
        assert last["next_cursor"] is None
        # --8<-- [end:cursor-calls]

        # A page that ends exactly on the last row reports no next page.
        exact = await article_page(cursor="2026-01-12_3", limit=2)
        assert [article.title for article in exact["items"]] == [
            "Indexes that pay off",
            "Choosing a primary key",
        ]
        assert exact["next_cursor"] is None

    print("pagination example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
