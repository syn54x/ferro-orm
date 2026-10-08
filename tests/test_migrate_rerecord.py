# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Edited step files and ``ferro migrate rerecord`` (#537, ADR-0030).

```text
$ ferro migrate up
ferro migrate: 0001_create_author/01_schema.up.sqlite.sql was edited after it was applied ...
`ferro migrate rerecord 0001:01`. Nothing was applied.
$ ferro migrate rerecord 0001:01
re-recorded 0001_create_author/01_schema.up.sqlite.sql (sha384:… → sha384:…); nothing was run
```

One test per row of the edited-file table, each on both dialects, run
against a real project under ``tmp_path`` and read back from the tracking
table with a plain driver:

- a finished step: ``up`` refuses; ``rerecord`` changes only the record;
- an unfinished transactional DDL, atomic data or no-transaction step:
  accepted, re-recorded, and ``up`` says so;
- an unfinished chunked step with committed batches: refused until
  ``rerecord --continue`` (same order keys) or ``--restart``;
- a step with no record: free to edit; a snapshot: never re-recorded;
- a ``down`` file or another dialect's rendering: not this database's
  checksum, so no mismatch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ferro.migrations import runner
from ferro.migrations.report import RunRefused
from ferro.settings import FerroSettings
from tests.test_chunked_steps import (
    STEP,
    failed_at_batch_two,
    project_with_authors,
    record,
    slugged,
    step_file,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
    run_report,
    sha384,
    short_time,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

EVERY_COLUMN = (
    "migration, step, migration_name, file, kind, checksum, snapshot_checksum, "
    "started_at, finished_at, failed_at, error, resume_cursor, rows_done, "
    "duration_ms, ferro_version, origin, reverting, revert_cursor"
)
CHECKSUM = 5


def every_column(db, migration: int, step: int) -> tuple:
    rows = db.rows(
        f"SELECT {EVERY_COLUMN} FROM _ferro_migrations "
        f"WHERE migration = {migration} AND step = {step}"
    )
    assert len(rows) == 1
    return rows[0]


def checksum_of(db, migration: int, step: int) -> str:
    return every_column(db, migration, step)[CHECKSUM]


def cli(*argv: str, capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def applied_author(project: Path, pkg: str, db) -> Path:
    """``0001_create_author`` applied; returns its up file."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    return migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"


# -- a finished step ------------------------------------------------------------------


def test_a_finished_step_edited_is_refused_and_rerecord_changes_only_its_checksum(
    project, pkg, db, capsys
):
    up_file = applied_author(project, pkg, db)
    applied = sha384(up_file)
    before = every_column(db, 1, 1)
    # The edit would create a table if anything ran it.
    up_file.write_bytes(
        up_file.read_bytes() + b'CREATE TABLE "marker" ("id" integer);\n'
    )
    edited = sha384(up_file)
    tables = db.tables()

    code, _, err = cli("up", "--url", db.url, capsys=capsys)
    assert code == 1
    assert err == (
        f"ferro migrate: 0001_create_author/{up_file.name} was edited after it was "
        f"applied to this database.\n"
        f"  applied   sha384:{applied}  ({short_time(before[8])})\n"
        f"  on disk   sha384:{edited}\n"
        f"An applied step is never run again. Restore the file, or accept a deliberate "
        f"edit with\n`ferro migrate rerecord 0001:01`. Nothing was applied.\n"
    )
    assert checksum_of(db, 1, 1) == applied

    code, out, err = cli("rerecord", "0001:01", "--url", db.url, capsys=capsys)
    assert (code, err) == (0, "")
    assert out == (
        f"re-recorded 0001_create_author/{up_file.name} (sha384:{applied} → "
        f"sha384:{edited}); nothing was run\n"
    )
    after = every_column(db, 1, 1)
    assert after[CHECKSUM] == edited
    assert after[:CHECKSUM] + after[CHECKSUM + 1 :] == (
        before[:CHECKSUM] + before[CHECKSUM + 1 :]
    ), "rerecord changes the checksum and nothing else"
    assert db.tables() == tables and "marker" not in tables, "no SQL ran"

    code, out, _ = cli("up", "--url", db.url, capsys=capsys)
    assert (code, out) == (0, "nothing to apply: the database is up to date\n")
    assert "marker" not in db.tables()


def test_the_refusal_is_structured(project, pkg, db):
    up_file = applied_author(project, pkg, db)
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    settings = FerroSettings()

    report = asyncio.run(
        run_report(runner.up(settings, settings.database(), url=db.url))
    )

    assert isinstance(report.refused, RunRefused)
    assert (report.refused.kind, report.refused.migration, report.refused.step) == (
        "edited_applied",
        1,
        1,
    )
    assert str(report.refused) == report.refusal


# -- unfinished steps are accepted and re-recorded --------------------------------------


def accepted(db, capsys, shown: str, recorded: str, on_disk: str) -> str:
    """Run ``up`` over an edited unfinished step: it says so, applies it,
    and the record finishes under the new checksum."""
    code, out, err = cli("up", "--url", db.url, capsys=capsys)
    assert (code, err) == (0, ""), err
    assert out.splitlines()[0] == (
        f"re-recorded {shown} (sha384:{recorded} → sha384:{on_disk})"
    )
    return out


def test_an_unfinished_transactional_ddl_step_edited_is_accepted(
    project, pkg, db, capsys
):
    applied_author(project, pkg, db)
    up_file = sql_step(
        project,
        "fix",
        'CREATE TABLE "fixed" ("id" integer);\nSELECT * FROM "missing";\n',
    )
    assert run("migrate", "up", "--url", db.url) == 1
    recorded = checksum_of(db, 2, 1)
    assert recorded == sha384(up_file)
    assert every_column(db, 2, 1)[8] is None, "unfinished"

    up_file.write_text('CREATE TABLE "fixed" ("id" integer);\n')
    out = accepted(db, capsys, f"0002_fix/{up_file.name}", recorded, sha384(up_file))

    assert "0002_fix  01_fix  applied (" in out
    after = every_column(db, 2, 1)
    assert (after[4], after[CHECKSUM], after[8] is not None) == (
        "ddl",
        sha384(up_file),
        True,
    )
    assert "fixed" in db.tables()


def test_an_unfinished_no_transaction_step_edited_is_accepted(project, pkg, db, capsys):
    if db.backend == "sqlite":
        pytest.skip("no-transaction is Postgres-only (ADR-0034); SQLite refuses it")
    applied_author(project, pkg, db)
    up_file = sql_step(
        project,
        "loose",
        '-- ferro: no-transaction\nCREATE TABLE "loose" ("id" integer);\n'
        'SELECT * FROM "missing";\n',
    )
    assert run("migrate", "up", "--url", db.url) == 1
    recorded = checksum_of(db, 2, 1)
    assert "loose" in db.tables(), "the statement before the failure stayed applied"

    up_file.write_text(
        '-- ferro: no-transaction\nCREATE TABLE IF NOT EXISTS "loose" ("id" integer);\n'
    )
    accepted(db, capsys, f"0002_loose/{up_file.name}", recorded, sha384(up_file))

    after = every_column(db, 2, 1)
    assert (after[4], after[CHECKSUM], after[8] is not None) == (
        "ddl-no-transaction",
        sha384(up_file),
        True,
    )


ATOMIC = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
        author.slug = {slug}
        await author.save()


@nothing_to_reverse("slugs are derived")
def down(ctx): ...
"""


def test_an_unfinished_atomic_step_edited_is_accepted(project, pkg, db, capsys):
    project_with_authors(project, pkg, db, authors=10)
    step = step_file(project)
    step.write_text(ATOMIC.format(slug="1 / 0"))
    assert run("migrate", "up", "--url", db.url) == 1
    recorded = checksum_of(db, 2, 2)
    assert every_column(db, 2, 2)[4] == "atomic"
    assert slugged(db) == 0, "the atomic step rolled back whole"

    step.write_text(ATOMIC.format(slug='author.name.lower().replace(" ", "-")'))
    accepted(db, capsys, f"0002_add_slug/{step.name}", recorded, sha384(step))

    assert slugged(db) == 10
    after = every_column(db, 2, 2)
    assert (after[4], after[CHECKSUM], after[8] is not None) == (
        "atomic",
        sha384(step),
        True,
    )


# -- an unfinished chunked step with committed batches ----------------------------------

ORDERED_BY_NAME = STEP.replace(
    ".order_by(lambda author: author.id),\n    batch_size=1000,\n)\nasync def up",
    ".order_by(lambda author: author.name)\n    .order_by(lambda author: author.id),\n"
    "    batch_size=1000,\n)\nasync def up",
    1,
)
EVERY_ROW = STEP.replace(
    "models.Author.where(lambda author: author.slug == None)\n    .order_by",
    "models.Author.select()\n    .order_by",
    1,
)


def edited_chunked(project, pkg, db, body: str | None = None) -> tuple[Any, str, str]:
    """The chunked backfill failed at batch 2 (1,000 rows committed), then
    edited; returns the probe, the recorded checksum and the edited one."""
    probe = failed_at_batch_two(project, pkg, db)
    recorded = checksum_of(db, 2, 2)
    step = step_file(project)
    step.write_text(
        body.format(pkg=pkg) if body is not None else step.read_text() + "# edited\n"
    )
    probe.reset()
    return probe, recorded, sha384(step)


def doors(rows: str = "1,000") -> str:
    return (
        f"ferro migrate: 0002_add_slug/02_backfill_author.py was edited after {rows} "
        f"rows were committed.\nFerro cannot tell whether those rows are right under "
        f"the new code. Choose one:\n"
        f"  ferro migrate rerecord 0002:02 --continue   keep them, continue from the "
        f"cursor\n"
        f"  ferro migrate rerecord 0002:02 --restart    run every row again from the "
        f"start\nNothing was applied.\n"
    )


def test_an_edited_chunked_step_is_refused_until_continued_from_its_cursor(
    project, pkg, db, capsys
):
    probe, recorded, edited = edited_chunked(project, pkg, db)
    before = record(db)
    assert before["resume_cursor"] == {
        "keys": [1000],
        "order_by": ["author.id"],
        "rows_done": 1000,
    }

    code, _, err = cli("up", "--url", db.url, capsys=capsys)
    assert (code, err) == (1, doors())
    code, _, err = cli("rerecord", "0002:02", "--url", db.url, capsys=capsys)
    assert (code, err) == (1, doors())
    assert record(db) == before and checksum_of(db, 2, 2) == recorded

    code, out, err = cli(
        "rerecord", "0002:02", "--continue", "--url", db.url, capsys=capsys
    )
    assert (code, err) == (0, "")
    assert out == (
        f"re-recorded 0002_add_slug/02_backfill_author.py (sha384:{recorded} → "
        f"sha384:{edited}); the next up continues from its cursor; nothing was run\n"
    )
    assert record(db) == before, "the cursor and rows_done stay"
    assert checksum_of(db, 2, 2) == edited
    assert probe.batches == []

    code, out, err = cli("up", "--url", db.url, capsys=capsys)
    assert (code, err) == (0, "")
    assert probe.writes["up"] == list(range(1001, 2501)), "resumed from the cursor"
    assert record(db)["rows_done"] == 2500
    assert slugged(db) == 2500


def test_continuing_over_changed_order_keys_is_refused_naming_both(
    project, pkg, db, capsys
):
    _, recorded, _ = edited_chunked(project, pkg, db, ORDERED_BY_NAME)
    before = record(db)

    code, _, err = cli("up", "--url", db.url, capsys=capsys)
    assert code == 1
    assert err == (
        "ferro migrate: 0002_add_slug/02_backfill_author.py was edited after 1,000 "
        "rows were committed.\nFerro cannot tell whether those rows are right under "
        "the new code, and the edited file pages over different order keys than its "
        "cursor:\n  cursor    author.id\n  on disk   author.name, author.id\nso the "
        "cursor is no position in it. Restart from the first row:\n"
        "  ferro migrate rerecord 0002:02 --restart    run every row again from the "
        "start\nNothing was applied.\n"
    )
    code, _, err = cli(
        "rerecord", "0002:02", "--continue", "--url", db.url, capsys=capsys
    )
    assert code == 1
    assert err == (
        "ferro migrate: cannot continue 0002_add_slug/02_backfill_author.py from its "
        "cursor: the edited file pages over different order keys than its cursor.\n"
        "  cursor    author.id\n  on disk   author.name, author.id\nThe cursor is no "
        "position in the edited query. Restart it with `ferro migrate rerecord "
        "0002:02 --restart`. Nothing was changed.\n"
    )
    assert record(db) == before and checksum_of(db, 2, 2) == recorded


def test_restarting_an_edited_chunked_step_runs_every_row_from_the_first(
    project, pkg, db, capsys
):
    probe, recorded, edited = edited_chunked(project, pkg, db, EVERY_ROW)

    code, out, err = cli(
        "rerecord", "0002:02", "--restart", "--url", db.url, capsys=capsys
    )
    assert (code, err) == (0, "")
    assert out == (
        f"re-recorded 0002_add_slug/02_backfill_author.py (sha384:{recorded} → "
        f"sha384:{edited}); its cursor and rows_done are cleared, so the next up "
        f"starts from the first row; nothing was run\n"
    )
    restarted = record(db)
    assert (restarted["resume_cursor"], restarted["rows_done"]) == (None, 0)
    assert checksum_of(db, 2, 2) == edited

    code, _, err = cli("up", "--url", db.url, capsys=capsys)
    assert (code, err) == (0, "")
    assert probe.writes["up"] == list(range(1, 2501)), "from the first row"
    assert record(db)["rows_done"] == 2500


def test_a_flag_on_a_step_it_does_not_apply_to_is_refused(project, pkg, db, capsys):
    up_file = applied_author(project, pkg, db)
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    applied = checksum_of(db, 1, 1)

    code, _, err = cli(
        "rerecord", "0001:01", "--restart", "--url", db.url, capsys=capsys
    )

    assert code == 1
    assert err == (
        "ferro migrate: --restart applies only to an unfinished chunked step with "
        "committed batches, and 0001:01 is finished.\nAccept its edit with `ferro "
        "migrate rerecord 0001:01`. Nothing was changed.\n"
    )
    assert checksum_of(db, 1, 1) == applied


def test_a_chunked_order_key_through_a_relation_is_refused_at_load():
    # Refused when the step is checked, before anything runs, rather than as
    # a KeyError when a resumed run decodes its cursor (#532 review).
    from types import SimpleNamespace

    from ferro.migrations.steps import Chunked, StepRefused, chunked_query
    from tests.test_position_paging_traversal import PosTravTransaction

    shape = Chunked(
        lambda models: (
            models.PosTravTransaction.select()
            .order_by(lambda txn: txn.account.label)
            .order_by(lambda txn: txn.id)
        ),
        100,
    )
    models = SimpleNamespace(PosTravTransaction=PosTravTransaction)
    path = Path("migrations/0002_backfill/02_backfill_txn.py")

    with pytest.raises(StepRefused) as refused:
        chunked_query(shape, models, path)  # type: ignore[arg-type]

    assert str(refused.value) == (
        "ferro migrate: 0002_backfill/02_backfill_txn.py: @chunked orders "
        "postravtransaction by account.label, a column of a related model; the "
        "cursor holds the last row's own order keys, so order by a column of the "
        "model, including its primary key: "
        ".order_by(lambda postravtransaction: postravtransaction.id)"
    )


# -- free to edit, never re-recorded, not this database's checksum ---------------------


def test_a_step_with_no_record_is_free_to_edit(project, pkg, db, capsys):
    applied_author(project, pkg, db)
    sql_step(project, "later", 'CREATE TABLE "later" ("id" integer);\n')

    code, _, err = cli("rerecord", "0002:01", "--url", db.url, capsys=capsys)

    assert code == 1
    assert err == (
        "ferro migrate: nothing to re-record at 0002:01: the step has no record on this "
        "database, so its file is free to edit. Nothing was changed.\n"
    )


@pytest.mark.parametrize("target", ["0001", "0001:ir"])
def test_rerecord_names_one_step_and_never_the_snapshot(
    project, pkg, db, capsys, target
):
    applied_author(project, pkg, db)
    snapshot = migrations(project) / "0001_create_author/ir.json"
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")
    before = every_column(db, 1, 1)

    code, _, err = cli("rerecord", target, "--url", db.url, capsys=capsys)

    assert code == 1
    assert err == (
        f"ferro migrate: rerecord re-records one step, named <migration>:<step> "
        f"(0007:01), not `{target}`.\nA schema snapshot (ir.json) is never "
        f"re-recorded: later migrations and historical models are built from it, so "
        f"restore the file. Nothing was changed.\n"
    )
    code, _, err = cli("rerecord", "0001:01", "--url", db.url, capsys=capsys)
    assert code == 1
    assert "0001_create_author/ir.json is not the snapshot this database applied" in err
    assert err.endswith("Restore the file. Nothing was applied.\n")
    assert every_column(db, 1, 1) == before


def test_a_down_file_or_another_dialects_rendering_is_not_a_mismatch(
    project, pkg, db, capsys
):
    other = "sqlite" if db.backend == "postgres" else "postgres"
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = ["{db.backend}", "{other}"]\n'
    )
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    applied = checksum_of(db, 1, 1)
    migration = migrations(project) / "0001_create_author"
    for edited in (
        migration / f"01_schema.down.{db.backend}.sql",
        migration / f"01_schema.up.{other}.sql",
    ):
        edited.write_bytes(edited.read_bytes() + b"-- edited\n")

    code, out, err = cli("up", "--url", db.url, capsys=capsys)
    assert (code, out, err) == (0, "nothing to apply: the database is up to date\n", "")
    code, _, err = cli("rerecord", "0001:01", "--url", db.url, capsys=capsys)
    assert code == 1
    assert err.startswith("ferro migrate: nothing to re-record at 0001:01: ")
    assert "matches its record" in err
    assert checksum_of(db, 1, 1) == applied


def test_status_shows_the_edited_chunked_refusal_with_its_doors(
    project, pkg, db, capsys
):
    edited_chunked(project, pkg, db)

    code, out, _ = cli("status", "--url", db.url, capsys=capsys)

    assert code == 4
    assert doors().rstrip("\n") in out


def test_the_cursor_names_its_order_keys():
    from ferro.migrations.chunked import encode_cursor

    assert json.loads(encode_cursor((7,), 7, order_by=["author.id"])) == {
        "keys": [7],
        "order_by": ["author.id"],
        "rows_done": 7,
    }


def test_rerecord_refuses_an_unknown_mode():
    from ferro.migrations.rerecord import rerecord
    from ferro.settings import SettingsError

    with pytest.raises(SettingsError, match="rerecord mode 'again'"):
        asyncio.run(
            rerecord(
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                "0001:01",
                mode="again",  # type: ignore[arg-type]
            )
        )
