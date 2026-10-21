"""The Auto-migrate page's label addition: ``Plan`` gained ``PRO``, and the
database is from before.

The models are ``docs/examples/enum_labels.py``'s. A block marked
``{test=run prelude=label_addition backend=postgres}`` runs on PostgreSQL,
where ``opened()`` first builds what an earlier release built: the native
type ``plan`` with only ``'free'``, and ``author`` with a ``plan`` column of
it, as that release's create pass left them. It does not leave a
connection open: the block connects itself, with the flag it is about.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ferro  # noqa: E402
from enum_labels import Author, Plan  # noqa: E402, F401 - names the blocks use
from ferro import engines, execute, fetch_all  # noqa: E402, F401

BEFORE = [
    """CREATE TYPE "plan" AS ENUM ('free')""",
    """CREATE TABLE "author" ( "email" varchar NOT NULL, "id" serial PRIMARY KEY"""
    """ NOT NULL, "name" varchar NOT NULL, "nickname" varchar, "plan" plan NOT NULL )""",
    """CREATE INDEX "idx_author_nickname" ON "author" ("nickname")""",
    """CREATE UNIQUE INDEX "uq_author_email" ON "author" ("email")""",
]
"""What the create pass sends for ``Author`` while ``Plan`` has only ``FREE``."""


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Build the earlier release's schema, then run the block."""
    await ferro.connect("postgresql://localhost/app")
    async with engines.session():
        for statement in BEFORE:
            await execute(statement)
    ferro.reset_engine()
    try:
        yield
    finally:
        ferro.reset_engine()
