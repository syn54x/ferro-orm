"""The running example of the Auto-migrate page: ``Author`` after it gained
``nickname``, and the database from before.

``opened()`` does not connect: each block on that page connects itself, with
the flag it is about. It runs the block in a scratch directory holding
``dev.db``, the SQLite file an earlier release built: ``author`` without
``nickname``, with a ``bio`` column the model has since dropped, and one row,
Ada Lovelace's. A block that wants a brand-new database connects to another
file in the same directory.
"""

import os
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import ferro
from ferro import Field, Model, engines  # noqa: F401 - names the blocks use


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    nickname: str | None = Field(default=None, index=True)


BEFORE = """
CREATE TABLE "author" (
    "bio" varchar,
    "email" varchar NOT NULL,
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "name" varchar NOT NULL
);
CREATE UNIQUE INDEX "uq_author_email" ON "author" ("email");
INSERT INTO "author" ("name", "email", "bio")
VALUES ('Ada Lovelace', 'ada@example.com', 'Wrote the first program.');
"""


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Run the block in a scratch directory holding the earlier release's ``dev.db``."""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as scratch:
        connection = sqlite3.connect(Path(scratch) / "dev.db")
        connection.executescript(BEFORE)
        connection.close()
        os.chdir(scratch)
        try:
            yield
        finally:
            ferro.reset_engine()
            os.chdir(cwd)
