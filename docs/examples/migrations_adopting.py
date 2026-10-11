# ruff: noqa: E402 - the models come first, as the other migrations examples
"""Runnable companion for adopting migrations at start-up
(docs/pages/howto/adopting-migrations.md, "An app that migrates at start-up").

The previous release built its SQLite file with auto-migrate; this release
generates ``0001`` from the same models and runs the page's ``start()``:

- in two app processes at once on that file: both call ``baseline()`` on it
  untracked, the run lock lets one record, and the other meets
  ``AlreadyTrackedError`` and carries on to ``up()``;
- on a brand-new file: the baseline finds nothing to match and records
  nothing, and ``up()`` creates it.

``python docs/examples/migrations_adopting.py --instance <url> <name>`` is
one app process (the example starts two of them itself).
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str


# --8<-- [end:models]


# --8<-- [start:recipe]
import ferro
import ferro.migrations


async def start(database_url: str) -> None:
    await ferro.connect(database_url)
    try:
        # A file the previous release built matches 0001 and is recorded; a
        # brand-new, empty file matches nothing and stays unrecorded, and
        # up() creates it.
        await ferro.migrations.baseline(target="0001")
    except ferro.migrations.AlreadyTrackedError:
        pass  # already tracked: an earlier start, or another instance, got here first
    await ferro.migrations.up()


# --8<-- [end:recipe]


import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from _migrations_project import project

RECORDS = "SELECT migration, origin FROM _ferro_migrations ORDER BY migration"
INSTANCES = ("first", "second")


def instance(url: str, name: str) -> None:
    """One app process starting: ``start(url)`` as the page writes it, its
    ``baseline()`` held back until every instance has reached it (so both
    call it on an untracked file), saying on stdout what it caught."""
    baseline = ferro.migrations.baseline

    async def together_then_baseline(*args, **kwargs):
        Path(f"{name}.ready").touch()
        while not all(Path(f"{other}.ready").exists() for other in INSTANCES):
            await asyncio.sleep(0.05)
        try:
            return await baseline(*args, **kwargs)
        except ferro.migrations.AlreadyTrackedError as err:
            print(f"AlreadyTrackedError head={err.head}", flush=True)
            raise

    ferro.migrations.baseline = together_then_baseline
    asyncio.run(start(url))


def rows(path: Path, statement: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        found = connection.execute(statement).fetchall()
    connection.close()
    return found


def main() -> None:
    with project(__file__) as app:
        app.models_after()
        app.ferro("migrate", "new", "create_author")

        async def previous_release() -> None:
            # The file as the release before migrations left it.
            await ferro.connect(app.url, auto_migrate=True)

        asyncio.run(previous_release())
        ferro.reset_engine()
        app.sql("INSERT INTO author (name) VALUES ('Ada Lovelace')")

        # Two app processes start together, in the project directory (the
        # recipe reads its configuration from there).
        script = str(Path(__file__).resolve())
        started = [
            subprocess.Popen(
                [sys.executable, script, "--instance", app.url, name],
                cwd=app.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in INSTANCES
        ]
        deadline = time.monotonic() + 60
        outputs = [p.communicate(timeout=deadline - time.monotonic()) for p in started]
        for process, (out, err) in zip(started, outputs, strict=True):
            assert process.returncode == 0, outputs
        caught = [line for out, _ in outputs for line in out.splitlines()]
        assert caught == ["AlreadyTrackedError head=0001_create_author"], outputs
        assert rows(app.root / "app.db", RECORDS) == [(1, "baseline")]
        assert rows(app.root / "app.db", "SELECT name FROM author") == [
            ("Ada Lovelace",)
        ]

        cwd = os.getcwd()
        os.chdir(app.root)
        try:
            # The next start: the file is tracked, so baseline() refuses and
            # up() finds nothing to apply.
            asyncio.run(start(app.url))
            ferro.reset_engine()
            assert rows(app.root / "app.db", RECORDS) == [(1, "baseline")]

            # A brand-new file: nothing to baseline, up() creates it.
            asyncio.run(start(f"sqlite:{app.root / 'fresh.db'}?mode=rwc"))
            ferro.reset_engine()
        finally:
            os.chdir(cwd)
        assert rows(app.root / "fresh.db", RECORDS) == [(1, "run")]


if __name__ == "__main__":
    if sys.argv[1:2] == ["--instance"]:
        instance(sys.argv[2], sys.argv[3])
    else:
        main()
