"""Annotated-style companion to quickstart.py (AGENTS.md I-8).

Same models as docs/examples/quickstart.py, declared with ``Annotated``
metadata instead of assignment. Field options move into ``Annotated[...]``;
relationship declarations are identical in both styles.
"""

import asyncio

# --8<-- [start:models]
from datetime import datetime
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect


class Author(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    email: Annotated[str, Field(unique=True)]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    title: str
    body: str
    published: bool = False
    created_at: Annotated[datetime, Field(default_factory=datetime.now)]
    author: Annotated[Author, ForeignKey(related_name="posts")]
# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    alice = await Author.create(name="Alice", email="alice@example.com")
    post = await Post.create(title="Hello", body="...", published=True, author=alice)

    assert post.id is not None
    assert (await post.author).email == "alice@example.com"
    assert len(await alice.posts.where(lambda post: post.published == True).all()) == 1  # noqa: E712

    print("quickstart_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
