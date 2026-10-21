"""Runnable companion for the errors and warnings the Auto-migrate guide quotes
(docs/pages/guide/schema/auto-migrate.md, docs/pages/reference/configuration.md).

Each region brings a database to the state the page describes, connects, and
prints what Ferro raises or warns. With no arguments it runs on SQLite files in
a temporary directory. ``python docs/examples/auto_migrate_errors.py <postgres-url>``
runs the Postgres scenario instead: a server default no model declares.
"""

import asyncio
import os
import sys
import tempfile
import warnings
from enum import StrEnum
from pathlib import Path

from ferro import (
    Field,
    Model,
    clear_registry,
    connect,
    engines,
    execute,
    reset_engine,
)


class Kind(StrEnum):
    PERSON = "person"
    COMPANY = "company"


def declare_author(release: str) -> type[Model]:
    """Declare ``Author`` as the release ``release`` has it.

    Every release declares the same model, so each declaration replaces the
    last, as a new deploy of the app would.
    """
    reset_engine()
    clear_registry()
    if release == "base":

        class Author(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str

    elif release == "required-bio":

        class Author(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            bio: str  # required, with no default

    elif release == "kind":

        class Author(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            kind: Kind = Field(default=Kind.PERSON, db_type="text")

    elif release == "checked-kind":

        class Author(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            kind: Kind = Field(default=Kind.PERSON, db_type="text", db_check=True)

    elif release == "slug":

        class Author(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str
            slug: str

    else:
        raise ValueError(release)
    return Author


async def required_column(url: str) -> None:
    declare_author("base")
    await connect(url, auto_migrate=True)
    declare_author("required-bio")
    # --8<-- [start:required-column]
    try:
        await connect(url, migrate_updates=True)
    except ValueError as error:
        print(error)
    # --8<-- [end:required-column]


async def sqlite_check(url: str) -> None:
    declare_author("kind")
    await connect(url, auto_migrate=True)
    declare_author("checked-kind")
    # --8<-- [start:sqlite-check]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect(url, migrate_updates=True)
    for warning in caught:
        print(warning.message)
    # --8<-- [end:sqlite-check]


async def run_lock(root: Path, url: str) -> None:
    import fcntl

    declare_author("base")
    (root / "ferro.toml").write_text(
        'models = ["__main__"]\ndialects = ["sqlite"]\nlock_timeout = "1s"\n'
    )
    # Another process's migration run holds the SQLite run lock: an OS lock on
    # the database's .ferro-migrate.lock file.
    with open(root / "app.db.ferro-migrate.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                await connect(url, auto_migrate=True)
            except Exception as error:  # noqa: BLE001 - printed below
                gave_up = error
            else:
                raise AssertionError("the pass ran while the lock was held")
    # --8<-- [start:lock-wait]
    for warning in caught:
        print(warning.message)
    # --8<-- [end:lock-wait]
    # --8<-- [start:lock-timeout]
    print(gave_up)
    # --8<-- [end:lock-timeout]
    (root / "ferro.toml").unlink()


async def foreign_default(url: str) -> None:
    declare_author("slug")
    await connect(url, auto_migrate=True)
    async with engines.session():
        await execute('ALTER TABLE "author" ALTER COLUMN "slug" SET DEFAULT \'\'')
    declare_author("slug")
    # --8<-- [start:foreign-default]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await connect(url, migrate_updates=True)
    for warning in caught:
        print(warning.message)
    # --8<-- [end:foreign-default]


async def main(argv: list[str]) -> None:
    if argv:
        await foreign_default(argv[0])
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.chdir(root)
        await required_column(f"sqlite:{root / 'required.db'}?mode=rwc")
        await sqlite_check(f"sqlite:{root / 'check.db'}?mode=rwc")
        await run_lock(root, f"sqlite:{root / 'app.db'}?mode=rwc")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
