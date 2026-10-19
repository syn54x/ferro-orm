"""Runnable companion to the pagination how-to (docs/pages/howto/pagination.md).

Seven articles, newest first, paged three at a time: by page number with
``offset()``, by position with ``after()`` and ``before()``, and through
``article_page()``, the function the page's FastAPI endpoint calls.
"""

import asyncio

# --8<-- [start:model]
from datetime import date

from ferro import Field, Model, connect, engines


class Article(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    published_on: date = Field(index=True)


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


def titles(articles: list[Article]) -> list[str]:
    return [article.title for article in articles]


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
            [Article(title=title, published_on=day) for title, day in ARTICLES]
        )
        # --8<-- [end:session]

        # --8<-- [start:offset-call]
        page_two = await get_page(page=2, per_page=3)
        assert titles(page_two) == [
            "Keyset paging in practice",
            "Reading a query plan",
            "Indexes that pay for themselves",
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
        assert titles(first_page) == [
            "Testing with an in-memory database",
            "Storing time in UTC",
            "Soft deletes and unique keys",
        ]

        second_page = await newest.after(first_page[-1]).all()
        assert titles(second_page) == [
            "Keyset paging in practice",
            "Reading a query plan",
            "Indexes that pay for themselves",
        ]
        # --8<-- [end:after]

        # --8<-- [start:before]
        previous_page = await newest.before(second_page[0]).all()
        assert titles(previous_page) == titles(first_page)
        # --8<-- [end:before]

        # --8<-- [start:cursor-calls]
        first = await article_page(limit=3)
        assert first["next_cursor"] == "2026-01-26_5"

        second = await article_page(cursor=first["next_cursor"], limit=3)
        assert second["next_cursor"] == "2026-01-12_2"

        last = await article_page(cursor=second["next_cursor"], limit=3)
        assert titles(last["items"]) == ["Choosing a primary key"]
        assert last["next_cursor"] is None
        # --8<-- [end:cursor-calls]

        # A page that ends exactly on the last row reports no next page.
        exact = await article_page(cursor="2026-01-12_3", limit=2)
        assert titles(exact["items"]) == [
            "Indexes that pay for themselves",
            "Choosing a primary key",
        ]
        assert exact["next_cursor"] is None

    print("pagination example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
