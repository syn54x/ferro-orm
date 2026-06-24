"""Runnable companion to the Pagination how-to (docs/pages/howto/pagination.md)."""

import asyncio

from ferro import Field, Model, connect


class Article(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str


# --8<-- [start:offset]
async def get_page(page: int, per_page: int = 20) -> list[Article]:
    return (
        await Article.select()
        .order_by(Article.id)
        .limit(per_page)
        .offset((page - 1) * per_page)
        .all()
    )
# --8<-- [end:offset]


# --8<-- [start:keyset]
async def get_after(after_id: int | None, limit: int = 20) -> list[Article]:
    query = Article.select() if after_id is None else Article.where(lambda article: article.id > after_id)
    return await query.order_by(Article.id).limit(limit).all()
# --8<-- [end:keyset]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    await Article.bulk_create([Article(title=f"Article {i}") for i in range(1, 51)])

    page_two = await get_page(page=2, per_page=10)
    assert [a.title for a in page_two][:2] == ["Article 11", "Article 12"]

    first_batch = await get_after(after_id=None, limit=10)
    next_batch = await get_after(after_id=first_batch[-1].id, limit=10)
    assert next_batch[0].title == "Article 11"

    print("pagination example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
