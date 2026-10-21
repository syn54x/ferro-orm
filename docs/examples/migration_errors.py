# ruff: noqa: E402, F811 - the models come first, before then after, as the other migrations examples
"""Runnable companion for the errors the migrations pages quote
(docs/pages/guide/schema/deploying.md, docs/pages/guide/schema/overview.md).

A project on SQLite walks through the states those errors describe: a
database behind its migrations, one ahead of its checkout by an expand step
and then by a contract step, and a tracked database that ``connect()`` is
asked to auto-migrate. Each region prints the error Ferro raises.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    bio: str | None = None
    slug: str | None = None  # new


# --8<-- [end:models]

# --8<-- [start:without-bio]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str | None = None


# --8<-- [end:without-bio]

import asyncio
import os
import shutil
import sys

import ferro
import ferro.migrations
from _migrations_project import project


async def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        app.models_after()
        app.ferro("migrate", "new", "author_slug")
        os.chdir(app.root)
        sys.path.insert(0, str(app.root))
        await ferro.connect(app.url)

        # --8<-- [start:behind]
        try:
            await ferro.migrations.require_applied()
        except ferro.migrations.PendingMigrationsError as error:
            print(f"ferro.migrations.PendingMigrationsError: {error}")
        # --8<-- [end:behind]

        # Release 1 applies 0002_author_slug; release 0's checkout lacks it.
        app.ferro("migrate", "up")
        expand = app.migrations / "0002_author_slug"
        kept = app.root / "0002_author_slug"
        shutil.move(expand, kept)

        # --8<-- [start:ahead]
        try:
            await ferro.migrations.require_applied()
        except ferro.migrations.DatabaseAheadError as error:
            print(f"ferro.migrations.DatabaseAheadError: {error}")
        # --8<-- [end:ahead]

        # An expand step lets release 0 through.
        await ferro.migrations.require_applied(allow_ahead="expand-only")

        # A contract step does not.
        shutil.move(kept, expand)
        app.models_region("without-bio")
        app.ferro("migrate", "new", "drop_bio")
        app.ferro("migrate", "up")
        shutil.rmtree(app.migrations / "0003_drop_bio")

        # --8<-- [start:ahead-by-contract]
        try:
            await ferro.migrations.require_applied(allow_ahead="expand-only")
        except ferro.migrations.DatabaseAheadError as error:
            print(f"ferro.migrations.DatabaseAheadError: {error}")
        # --8<-- [end:ahead-by-contract]

        ferro.reset_engine()
        # --8<-- [start:tracked]
        try:
            await ferro.connect(app.url, auto_migrate=True)
        except ferro.migrations.MigrationRefused as error:
            print(error)
        # --8<-- [end:tracked]


if __name__ == "__main__":
    asyncio.run(main())
