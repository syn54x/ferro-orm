"""The running example for docstrings and the API reference.

Two authors and their posts, in an in-memory SQLite database. A block marked
``{test=run prelude=reference}`` runs inside ``opened()`` and can use
``Author`` and ``Post`` directly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    published: bool = False
    views: int = 0
    author: Annotated[Author, ForeignKey(related_name="posts")]


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the rows and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        ada = await Author.create(name="Ada Lovelace", email="ada@example.com")
        grace = await Author.create(name="Grace Hopper", email="grace@example.com")
        await Post.create(
            title="Notes on the Analytical Engine",
            published=True,
            views=1843,
            author=ada,
        )
        await Post.create(
            title="The Education of a Computer",
            published=True,
            views=952,
            author=grace,
        )
        yield
