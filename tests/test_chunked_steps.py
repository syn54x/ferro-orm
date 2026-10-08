# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Chunked data steps (#532, ADR-0024): a backfill in batches that resumes.

```python
@chunked(lambda models: models.Author.where(lambda author: author.slug == None)
                                     .order_by(lambda author: author.id), batch_size=1000)
async def up(ctx, batch):
    for author in batch:
        author.slug = author.name.lower().replace(" ", "-")
        await author.save()
```

The runner pages the declared query with keyset ``after()``, one
transaction per batch, and commits the batch's cursor and ``rows_done`` on
the step's record inside that same transaction. An interrupted step resumes
at its last committed batch and never replays a row; a chunked ``down`` keeps
the record ``reverting`` with its own cursor until its last batch.

Every test builds a real project under ``tmp_path`` with 2,500 authors and
runs the step in-process against the parametrized database (SQLite and
Postgres), reading rows and the tracking table back from a second, plain
driver connection. The step reports each batch to a ``probe`` module in the
project, which can pause it (an ``asyncio.Event``) or make a batch raise.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import ferro
from ferro.migrations import runner
from ferro.migrations.report import RunRefused
from ferro.migrations.chunked import decode_cursor, encode_cursor
from ferro.settings import FerroSettings
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import configure, db, migrations, new  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

MODELS = """
from ferro import ManyToMany


class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    authors: Relation[list["Author"]] = BackRef()


class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    tags: Relation[list[Tag]] = ManyToMany(related_name="authors")
"""

WITH_SLUG = MODELS + "    slug: str | None = None\n"

PROBE = """\
import asyncio

batches = []
writes = {"up": [], "down": []}
pause_on = None
fail_on = None
paused = None
release = None


def reset():
    global pause_on, fail_on, paused, release
    batches.clear()
    writes["up"].clear()
    writes["down"].clear()
    pause_on = fail_on = None
    paused, release = asyncio.Event(), asyncio.Event()


async def started(direction, batch):
    batches.append((direction, len(batch)))
    if pause_on == (direction, count(direction)):
        paused.set()
        await release.wait()


def finished(direction):
    if fail_on == (direction, count(direction)):
        raise RuntimeError(f"batch {count(direction)} gave up")


def count(direction):
    return sum(1 for d, _ in batches if d == direction)
"""

STEP = """\
from ferro.migrations import chunked
from {pkg} import probe


@chunked(
    lambda models: models.Author.where(lambda author: author.slug == None)
    .order_by(lambda author: author.id),
    batch_size=1000,
)
async def up(ctx, batch):
    await probe.started("up", batch)
    for author in batch:
        author.slug = author.name.lower().replace(" ", "-")
        await author.save()
        probe.writes["up"].append(author.id)
    probe.finished("up")


@chunked(
    lambda models: models.Author.where(lambda author: author.slug != None)
    .order_by(lambda author: author.id),
    batch_size=1000,
)
async def down(ctx, batch):
    await probe.started("down", batch)
    for author in batch:
        author.slug = None
        await author.save()
        probe.writes["down"].append(author.id)
    probe.finished("down")
"""

RECORD = (
    "SELECT kind, finished_at, failed_at, error, resume_cursor, rows_done, "
    "reverting, revert_cursor FROM _ferro_migrations WHERE migration = 2 AND step = 2"
)


def project_with_authors(project: Path, pkg: str, db, authors: int = 2500) -> Any:
    """``0001_create_author`` applied holding ``authors`` rows, and
    ``0002_add_slug`` (the ``slug`` column plus the chunked backfill) written.
    Returns the project's ``probe`` module."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    if authors and db.backend == "postgres":
        db.execute(
            "INSERT INTO author (name) SELECT 'Author ' || i "
            f"FROM generate_series(1, {authors}) AS i"
        )
    elif authors:
        db.execute(
            f"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
            f"WHERE i < {authors}) INSERT INTO author (name) "
            f"SELECT 'Author ' || i FROM n"
        )
    write_models(project, pkg, WITH_SLUG)
    new("add_slug", "--data-step", "Author")
    step_file(project).write_text(STEP.format(pkg=pkg))
    (project / pkg / "probe.py").write_text(PROBE)
    sys.path.insert(0, str(project))
    return importlib.import_module(f"{pkg}.probe")


def step_file(project: Path) -> Path:
    return migrations(project) / "0002_add_slug" / "02_backfill_author.py"


def record(db) -> dict[str, Any] | None:
    rows = db.rows(RECORD)
    if not rows:
        return None
    keys = (
        "kind",
        "finished_at",
        "failed_at",
        "error",
        "resume_cursor",
        "rows_done",
        "reverting",
        "revert_cursor",
    )
    found = dict(zip(keys, rows[0], strict=True))
    found["reverting"] = bool(found["reverting"])
    for cursor in ("resume_cursor", "revert_cursor"):
        if found[cursor] is not None:
            found[cursor] = json.loads(found[cursor])
    return found


def slugged(db) -> int:
    return db.rows("SELECT count(*) FROM author WHERE slug IS NOT NULL")[0][0]


async def up(**kwargs: Any) -> runner.RunReport:
    settings = FerroSettings()
    return await runner.up(settings, settings.database(), **kwargs)


async def down(**kwargs: Any) -> runner.RunReport:
    settings = FerroSettings()
    return await runner.down(settings, settings.database(), **kwargs)


async def paused_at(probe: Any, task: asyncio.Task) -> None:
    waiter = asyncio.ensure_future(probe.paused.wait())
    done, _ = await asyncio.wait(
        {waiter, task}, timeout=60, return_when="FIRST_COMPLETED"
    )
    assert waiter in done, task.result() if task in done else "never paused"


# -- the happy path -------------------------------------------------------------------


def test_2500_rows_run_in_three_batches_each_committing_its_cursor(project, pkg, db):
    probe = project_with_authors(project, pkg, db)
    seen: dict[str, Any] = {}

    async def scenario() -> runner.RunReport:
        probe.reset()
        probe.pause_on = ("up", 2)
        task = asyncio.create_task(up(url=db.url))
        await paused_at(probe, task)
        # Batch 1 committed; batch 2 is inside its own transaction.
        seen["record"] = record(db)
        seen["slugged"] = slugged(db)
        probe.release.set()
        return await task

    report = asyncio.run(scenario())

    assert report.refusal is None
    assert [s.step for s in report.applied] == ["01_schema", "02_backfill_author"]
    assert probe.batches == [("up", 1000), ("up", 1000), ("up", 500)]
    assert seen["record"] == {
        "kind": "chunked",
        "finished_at": None,
        "failed_at": None,
        "error": None,
        "resume_cursor": {"keys": [1000], "order_by": ["author.id"], "rows_done": 1000},
        "rows_done": 1000,
        "reverting": False,
        "revert_cursor": None,
    }
    assert seen["slugged"] == 1000
    final = record(db)
    assert final["finished_at"] is not None and final["failed_at"] is None
    assert final["rows_done"] == 2500
    assert final["resume_cursor"] == {
        "keys": [2500],
        "order_by": ["author.id"],
        "rows_done": 2500,
    }
    assert slugged(db) == 2500
    assert db.rows("SELECT slug FROM author WHERE id = 7") == [("author-7",)]
    assert sorted(probe.writes["up"]) == list(range(1, 2501))


def test_zero_matching_rows_is_one_empty_pass_and_a_finished_record(project, pkg, db):
    probe = project_with_authors(project, pkg, db, authors=0)
    probe.reset()

    report = asyncio.run(up(url=db.url))

    assert report.refusal is None
    assert probe.batches == []
    final = record(db)
    assert final["finished_at"] is not None
    assert final["rows_done"] == 0
    assert final["resume_cursor"] is None


def test_a_lock_lost_mid_batch_rolls_that_batch_back_and_keeps_the_cursor(
    project, pkg, db
):
    """Each batch's cursor is written inside the batch's transaction once
    the lock is verified there (ADR-0029 as amended): a lock lost while
    batch 2 runs rolls batch 2 back, batch 1's 1,000 rows stay done, and the
    run stops as a lost lock."""
    if db.backend != "postgres":
        pytest.skip("only a Postgres lock lives on a connection that can drop")
    probe = project_with_authors(project, pkg, db)

    async def scenario() -> runner.RunReport:
        probe.reset()
        probe.pause_on = ("up", 2)
        await ferro.connect(db.url, name="walker")
        tracked = await runner.open_tracked("walker", FerroSettings().database())
        async with tracked.locked(5.0) as run:
            plan = await run.plan({"direction": "up"})
            task = asyncio.create_task(runner._walk(run, plan, None, using="walker"))
            await paused_at(probe, task)
            await run._close_lock_connection_for_test()
            probe.release.set()
            return await task

    report = asyncio.run(scenario())

    assert report.refused is not None and isinstance(report.refused, RunRefused)
    assert "the run lock was lost" in (report.refusal or "")
    assert slugged(db) == 1000, "batch 1 committed, batch 2 rolled back"
    stands = record(db)
    assert stands["finished_at"] is None and stands["failed_at"] is None
    assert stands["rows_done"] == 1000
    assert stands["resume_cursor"] == {
        "keys": [1000],
        "order_by": ["author.id"],
        "rows_done": 1000,
    }


# -- failure and resume ---------------------------------------------------------------


def failed_at_batch_two(project: Path, pkg: str, db) -> Any:
    probe = project_with_authors(project, pkg, db)
    probe.reset()
    probe.fail_on = ("up", 2)
    report = asyncio.run(up(url=db.url))
    assert report.refusal is not None
    assert (
        "ferro migrate: 0002_add_slug/02_backfill_author.py failed: RuntimeError: "
        "batch 2 gave up" in report.refusal
    )
    assert "1,000 rows" in report.refusal
    return probe


def test_a_failed_batch_rolls_back_alone_and_up_resumes_at_the_cursor(project, pkg, db):
    probe = failed_at_batch_two(project, pkg, db)

    assert slugged(db) == 1000, "batch 1 committed, batch 2 rolled back"
    failed = record(db)
    assert failed["finished_at"] is None and failed["failed_at"] is not None
    assert failed["error"] == "RuntimeError: batch 2 gave up"
    assert failed["rows_done"] == 1000
    assert failed["resume_cursor"] == {
        "keys": [1000],
        "order_by": ["author.id"],
        "rows_done": 1000,
    }
    first_run = list(probe.writes["up"])
    assert first_run == list(range(1, 2001))

    # A row written behind the cursor while the step stands part-way is not
    # visited by the rest of the run.
    db.execute("INSERT INTO author (id, name) VALUES (0, 'Late Comer')")

    probe.reset()
    report = asyncio.run(up(url=db.url))

    assert report.refusal is None
    assert [s.step for s in report.applied] == ["02_backfill_author"]
    resumed = probe.writes["up"]
    assert resumed == list(range(1001, 2501)), "no committed row replayed"
    assert probe.batches == [("up", 1000), ("up", 500)]
    final = record(db)
    assert final["finished_at"] is not None
    assert final["failed_at"] is None and final["error"] is None
    assert final["rows_done"] == 2500
    assert db.rows("SELECT slug FROM author WHERE id = 0") == [(None,)]
    assert slugged(db) == 2500


def test_status_shows_how_far_a_failed_chunked_step_got(project, pkg, db, capsys):
    failed_at_batch_two(project, pkg, db)
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 4

    out = capsys.readouterr().out
    assert re.search(r"\n  02_backfill_author\.py +failed at 1,000 rows\n", out)
    assert "    RuntimeError: batch 2 gave up\n" in out


# -- down ------------------------------------------------------------------------------


def test_a_chunked_down_reverts_in_batches_and_an_interrupted_one_blocks_up(
    project, pkg, db, capsys
):
    probe = project_with_authors(project, pkg, db)
    probe.reset()
    assert asyncio.run(up(url=db.url)).refusal is None
    seen: dict[str, Any] = {}

    async def killed_mid_down() -> None:
        probe.reset()
        probe.pause_on = ("down", 2)
        task = asyncio.create_task(down(target="0002:01", url=db.url))
        await paused_at(probe, task)
        seen["record"] = record(db)
        seen["slugged"] = slugged(db)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(killed_mid_down())

    assert seen["slugged"] == 1500, "the down's batch 1 committed"
    assert seen["record"]["reverting"] is True
    assert seen["record"]["revert_cursor"] == {
        "keys": [1000],
        "order_by": ["author.id"],
        "rows_done": 1000,
    }
    assert seen["record"]["finished_at"] is not None
    assert seen["record"]["resume_cursor"] == {
        "keys": [2500],
        "order_by": ["author.id"],
        "rows_done": 2500,
    }
    # Killed inside batch 2: it rolled back, the record stands reverting.
    assert slugged(db) == 1500
    assert record(db)["reverting"] is True
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1
    assert (
        "ferro migrate: 0002_add_slug/02_backfill_author.py is part-way through being "
        "reverted" in capsys.readouterr().err
    )
    assert run("migrate", "status", "--url", db.url) == 4
    assert re.search(
        r"\n  02_backfill_author\.py +reverting\n", capsys.readouterr().out
    )

    probe.reset()
    report = asyncio.run(down(target="0002:01", url=db.url))

    assert report.refusal is None
    assert [s.step for s in report.reverted] == ["02_backfill_author"]
    assert probe.batches == [("down", 1000), ("down", 500)]
    assert probe.writes["down"] == list(range(1001, 2501)), "resumed at its cursor"
    assert record(db) is None
    assert slugged(db) == 0


def test_a_chunked_down_that_fails_after_a_batch_stays_reverting_with_the_error(
    project, pkg, db, capsys
):
    probe = project_with_authors(project, pkg, db)
    probe.reset()
    assert asyncio.run(up(url=db.url)).refusal is None
    probe.reset()
    probe.fail_on = ("down", 2)

    report = asyncio.run(down(target="0002:01", url=db.url))

    assert "RuntimeError: batch 2 gave up" in report.refusal
    reverting = record(db)
    assert reverting["reverting"] is True
    assert reverting["revert_cursor"] == {
        "keys": [1000],
        "order_by": ["author.id"],
        "rows_done": 1000,
    }
    assert reverting["error"] == "RuntimeError: batch 2 gave up"
    assert slugged(db) == 1500
    capsys.readouterr()
    assert run("migrate", "status", "--url", db.url) == 4
    out = capsys.readouterr().out
    assert re.search(r"\n  02_backfill_author\.py +reverting\n", out)
    assert "    RuntimeError: batch 2 gave up\n" in out


def test_a_down_that_fails_before_changing_anything_leaves_the_step_installed(
    project, pkg, db, capsys
):
    probe = project_with_authors(project, pkg, db)
    probe.reset()
    assert asyncio.run(up(url=db.url)).refusal is None
    probe.reset()
    probe.fail_on = ("down", 1)

    report = asyncio.run(down(target="0002:01", url=db.url))

    # The down's only batch rolled back: the database stands where it stood,
    # so the record does too, and the error is the run's to report.
    assert "RuntimeError: batch 1 gave up" in report.refusal
    standing = record(db)
    assert standing["reverting"] is False and standing["revert_cursor"] is None
    assert standing["finished_at"] is not None
    assert standing["failed_at"] is None and standing["error"] is None
    assert slugged(db) == 2500
    capsys.readouterr()
    assert run("migrate", "status", "--url", db.url) == 0


# -- refusals at load ----------------------------------------------------------------


def refused_at_load(project, pkg, db, capsys, query: str) -> str:
    project_with_authors(project, pkg, db, authors=0)
    step_file(project).write_text(
        textwrap.dedent(
            f"""\
            from ferro.migrations import chunked, nothing_to_reverse


            @chunked(lambda models: {query}, batch_size=100)
            async def up(ctx, batch): ...


            @nothing_to_reverse("test step")
            def down(ctx): ...
            """
        )
    )
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert "Nothing was applied." in err
    assert db.rows("SELECT count(*) FROM _ferro_migrations")[0][0] == 1
    return err


def test_a_query_over_a_join_table_is_refused_naming_the_parent_model_recipe(
    project, pkg, db, capsys
):
    err = refused_at_load(
        project,
        pkg,
        db,
        capsys,
        'models.table("author_tags").select().order_by(lambda link: link.author_id)',
    )

    assert "0002_add_slug/02_backfill_author.py: @chunked pages author_tags" in err
    assert "#492" in err
    assert "page the parent model" in err


def test_a_query_without_order_by_is_refused(project, pkg, db, capsys):
    err = refused_at_load(
        project,
        pkg,
        db,
        capsys,
        "models.Author.where(lambda author: author.slug == None)",
    )

    assert "0002_add_slug/02_backfill_author.py: @chunked needs an ordered query" in err
    assert ".order_by(lambda author: author.id)" in err


def test_a_query_ordered_without_its_primary_key_is_refused(project, pkg, db, capsys):
    err = refused_at_load(
        project,
        pkg,
        db,
        capsys,
        "models.Author.select().order_by(lambda author: author.name)",
    )

    assert (
        "0002_add_slug/02_backfill_author.py: @chunked orders author by name "
        "without its primary key" in err
    )
    assert ".order_by(lambda author: author.id)" in err


# -- the cursor codec ---------------------------------------------------------------


def test_cursor_values_round_trip_through_the_wire_canonical_form():
    import datetime as dt
    import uuid
    from decimal import Decimal

    from ferro._bind_payload import canonicalize_wire_scalar

    when = dt.datetime(2026, 3, 1, 15, 0, tzinfo=dt.UTC)
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    keys = (when, dt.date(2026, 3, 1), Decimal("1.50"), ident, None, 42)
    types = (dt.datetime, dt.date, Decimal, uuid.UUID, str | None, int)

    order_by = ["a.when", "a.day", "a.amount", "a.ident", "a.note", "a.id"]
    encoded = encode_cursor(keys, 2000, order_by=order_by)

    assert json.loads(encoded) == {
        "keys": [canonicalize_wire_scalar(key) for key in keys],
        "order_by": order_by,
        "rows_done": 2000,
    }
    assert json.loads(encoded)["keys"][0] == "2026-03-01T15:00:00Z"
    assert decode_cursor(encoded, types, order_by) == (keys, 2000)


# -- the batch transaction ------------------------------------------------------------


def test_an_immediate_transaction_holds_the_sqlite_write_lock_before_it_reads(db):
    """Each batch is ``transaction(immediate=True)``: on SQLite the write
    lock is held from ``BEGIN``, before the batch's first read."""

    import ferro

    if db.backend != "sqlite":
        pytest.skip("BEGIN IMMEDIATE is SQLite's; on Postgres immediate is a no-op")
    db.execute("CREATE TABLE chk_claim (id INTEGER PRIMARY KEY)")
    path = db.url.removeprefix("sqlite:").split("?")[0]

    def other_writer() -> str | None:
        # Another process: SQLite's file locks are per process, so a writer
        # in this one would never see the lock (and could corrupt the file).
        script = textwrap.dedent(
            f"""\
            import sqlite3
            conn = sqlite3.connect({path!r}, timeout=0, isolation_level=None)
            try:
                conn.execute("INSERT INTO chk_claim (id) VALUES (1)")
                conn.execute("DELETE FROM chk_claim")
            except sqlite3.OperationalError as err:
                print(err)
            """
        )
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        ).stdout.strip()
        return out or None

    async def scenario() -> tuple[str | None, str | None]:
        await ferro.connect(db.url, name="chk_immediate")
        try:
            async with ferro.transaction(using="chk_immediate", immediate=True):
                held = other_writer()
            async with ferro.transaction(using="chk_immediate"):
                deferred = other_writer()
        finally:
            await ferro._core._disconnect("chk_immediate")
        return held, deferred

    held, deferred = asyncio.run(scenario())

    assert held == "database is locked"
    assert deferred is None, "a deferred BEGIN takes no lock before its first statement"
