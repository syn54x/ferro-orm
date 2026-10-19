"""Annotated-style companion to faq.py (AGENTS.md I-7).

Same models as docs/examples/faq.py, with the field options inside
``Annotated[...]`` and the defaults assigned.
"""

import asyncio

# --8<-- [start:models]
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


class Author(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    email: Annotated[str, Field(unique=True)]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    title: str
    published: bool = False
    views: int = 0
    author: Annotated[Author, ForeignKey(related_name="posts")]


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await Author.create(name="Ada Lovelace", email="ada@example.com")
        await Post.create(title="Notes", published=True, views=10, author=ada)
        authors = await Author.where(lambda author: author.name == "Ada Lovelace").all()
        assert [author.email for author in authors] == ["ada@example.com"]
        posts = await Post.where(lambda post: post.views == 10).all()
        assert len(posts) == 1

    # --8<-- [start:databases]
    await connect("sqlite::memory:", name="analytics", auto_migrate=True)
    async with engines.session():
        authors = await Author.all()  # the default connection
    analytics_authors = await Author.using("analytics").all()  # explicit routing
    print(len(authors), len(analytics_authors))  # 1 0
    # --8<-- [end:databases]
    assert (len(authors), len(analytics_authors)) == (1, 0)

    print("faq annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
