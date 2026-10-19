"""The quickstart's models, with no connection: for the Connections guide.

A block marked ``{test=run prelude=connections}`` can use ``Author`` and
``Post`` and connects on its own, because connecting and opening sessions is
what the Connections guide shows. ``opened()`` connects nothing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    body: str
    published: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    author: Annotated[Author, ForeignKey(related_name="posts")]


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Run the block as it stands: it connects and opens its own sessions."""
    yield
