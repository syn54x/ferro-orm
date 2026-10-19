"""Annotated-style companion to quickstart.py (AGENTS.md I-7).

Same models as docs/examples/quickstart.py, declared with ``Annotated``
metadata instead of assignment. Field options move into ``Annotated[...]``;
the default stays as the assigned value. Relationship declarations are
identical in both styles.
"""

import asyncio

from ferro import connect, engines

# --8<-- [start:models]
from datetime import UTC, datetime
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


class Author(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    email: Annotated[str, Field(unique=True)]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    title: str
    body: str
    published: bool = False
    created_at: Annotated[datetime, Field(default_factory=lambda: datetime.now(UTC))]
    author: Annotated[Author, ForeignKey(related_name="posts")]


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        alice = await Author.create(name="Alice", email="alice@example.com")
        post = await Post.create(
            title="Hello", body="First post.", published=True, author=alice
        )

        assert post.id == 1
        assert (await post.author).email == "alice@example.com"
        published = await alice.posts.where(lambda post: post.published == True).all()  # noqa: E712
        assert len(published) == 1

    print("quickstart_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
