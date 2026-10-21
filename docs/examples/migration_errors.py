# ruff: noqa: E402, F811 - the models come first, before then after, as the other migrations examples
"""Runnable companion for the errors the migrations pages quote
(docs/pages/guide/schema/deploying.md, docs/pages/guide/schema/overview.md).

A project on SQLite walks through the two-release change the Deploying guide
tabulates: release 1 adds a nullable ``slug`` (one expand step), release 2
makes it required (backfill, add-constraint, contract). On the way it reaches
the states those errors describe: a database behind its migrations, one ahead
of its checkout by an expand step and then by a contract step, and a tracked
database that ``connect()`` is asked to auto-migrate. Each region prints the
error Ferro raises.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str | None = None  # new


# --8<-- [end:models]

# --8<-- [start:slug-required]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str


# --8<-- [end:slug-required]

import asyncio
import os
import shutil
import sys

import ferro
import ferro.migrations
from _migrations_project import project

TODO = '        author.slug = todo("the slug for an existing author")\n'
WRITTEN = '        author.slug = author.name.lower().replace(" ", "-")\n'


async def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.ferro("migrate", "up")
        # Release 1: a nullable slug, one expand step.
        app.models_after()
        app.ferro("migrate", "new", "author_slug_nullable")
        os.chdir(app.root)
        sys.path.insert(0, str(app.root))
        await ferro.connect(app.url)

        # --8<-- [start:behind]
        try:
            await ferro.migrations.require_applied()
        except ferro.migrations.PendingMigrationsError as error:
            print(f"{type(error).__name__}: {error}")
        # --8<-- [end:behind]

        # Release 1 applies 0002; release 0's checkout lacks it.
        app.ferro("migrate", "up")
        release_1 = app.migrations / "0002_author_slug_nullable"
        kept = app.root / release_1.name
        shutil.move(release_1, kept)

        # --8<-- [start:ahead]
        try:
            await ferro.migrations.require_applied()
        except ferro.migrations.DatabaseAheadError as error:
            print(f"{type(error).__name__}: {error}")
        # --8<-- [end:ahead]

        # An expand step lets release 0 through.
        await ferro.migrations.require_applied(allow_ahead="expand-only")

        # Release 2 makes slug required; release 1's checkout lacks 0003.
        shutil.move(kept, release_1)
        app.models_region("slug-required")
        app.ferro("migrate", "new", "author_slug_required")
        release_2 = app.migrations / "0003_author_slug_required"
        backfill = release_2 / "01_backfill_author.py"
        backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))
        app.ferro("migrate", "up")
        shutil.rmtree(release_2)

        # --8<-- [start:ahead-by-contract]
        try:
            await ferro.migrations.require_applied(allow_ahead="expand-only")
        except ferro.migrations.DatabaseAheadError as error:
            print(f"{type(error).__name__}: {error}")
        # --8<-- [end:ahead-by-contract]

        ferro.reset_engine()
        # --8<-- [start:tracked]
        try:
            await ferro.connect(app.url, auto_migrate=True)
        except ferro.migrations.MigrationRefused as error:
            print(f"{type(error).__name__}: {error}")
        # --8<-- [end:tracked]


if __name__ == "__main__":
    asyncio.run(main())
