# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""What ``ferro migrate up``, ``down``, ``baseline``, ``rerecord`` and ``init``
print, in both forms (#653), and the run events they print from.

```text
$ ferro migrate up
default · sqlite:app.db
  ✔ 0001_create_author  01_schema  applied  0.0s
  ✘ 0001_create_author  02_two_b   failed

✘ 02_two_b failed
  ferro migrate: 0001_create_author/02_two_b.up.sql failed: ...
```

Every line a run says is an event (:class:`~ferro.migrations.runner.StepEvent`
or :class:`~ferro.migrations.runner.RunNote`); ``progress`` receives each
event's ``line()``, and the CLI prints the event itself. Pytest's capture is
not a terminal, so a test with no environment set pins the **plain form**,
byte for byte what the string hook says; ``FORCE_COLOR=1`` and
``COLUMNS=100`` give the **TTY form**, pinned with the escapes Rich writes.
A duration is matched with a regular expression.
"""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import sys

import pytest

from ferro.migrations import baseline, runner
from ferro.migrations.rerecord import RerecordReport
from tests.test_chunked_steps import project_with_authors
from tests.test_cli_output_new_check import (  # noqa: F401 - fixtures
    no_bytecode,
    plain_env,
    tty,
)
from tests.test_cli_output_status_drift import sgr, tty_heading, tty_line
from tests.test_migrate_adopt import ADOPTED, TITLE, squashed
from tests.test_migrate_baseline import BOTH, NOT_COMPARED, auto_migrated
from tests.test_migrate_drift import SQUADS
from tests.test_migrate_rerecord import edited_chunked
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
    settings_and_database,
    sha384,
    sql_step,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode", "plain_env"
)


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def two_migrations(project, pkg, db) -> None:
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')


# -- the events -------------------------------------------------------------------


def said(events: list[runner.StepEvent | runner.RunNote]) -> list[str]:
    """What ``progress`` hears of ``events``: each one's line, a failed
    step's none (its message is the run's refusal)."""
    return [line for event in events if (line := event.line()) is not None]


@pytest.mark.backend_matrix
def test_progress_receives_each_events_line(project, pkg, db):
    two_migrations(project, pkg, db)
    settings, database = settings_and_database()
    lines: list[str] = []
    events: list[runner.StepEvent | runner.RunNote] = []

    asyncio.run(
        runner.up(
            settings,
            database.name,
            url=db.url,
            progress=lines.append,
            on_event=events.append,
        )
    )

    assert len(events) == len(lines) == 2
    assert said(events) == lines
    first, second = events
    assert isinstance(first, runner.StepEvent)
    assert (first.migration, first.file, first.outcome, first.reason) == (
        "0001_create_author",
        f"01_schema.up.{db.backend}.sql",
        "applied",
        None,
    )
    assert (first.name_width, first.stem_width) == (18, 9)
    assert first.line() == first.line(first.name_width, first.stem_width)
    assert first.line(20, 10) == (
        f"0001_create_author    01_schema   applied ({first.duration_ms} ms)"
    )
    assert lines[1] == (
        f"0002_second         01_second  applied ({second.duration_ms} ms)"
    )

    lines.clear()
    events.clear()
    asyncio.run(
        runner.down(
            settings,
            database.name,
            url=db.url,
            progress=lines.append,
            on_event=events.append,
        )
    )
    assert said(events) == lines
    assert [(e.migration, e.outcome) for e in events] == [("0002_second", "reverted")]
    assert lines == [f"0002_second  01_second  reverted ({events[0].duration_ms} ms)"]


def test_a_nothing_to_reverse_event_carries_its_reason():
    event = runner.StepEvent(
        migration="0002_add_team",
        file="02_backfill_account.py",
        outcome="nothing to reverse",
        duration_ms=None,
        reason="a data step with no down",
        name_width=13,
        stem_width=19,
    )
    assert event.line() == (
        "0002_add_team  02_backfill_account  "
        "nothing to reverse: a data step with no down"
    )
    assert runner.RunNote("re-recorded x").line() == "re-recorded x"


@pytest.mark.backend_matrix
def test_a_resumed_edited_step_is_a_note_event(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    fix = sql_step(project, "fix", 'INSERT INTO "missing" VALUES (1);\n')
    settings, database = settings_and_database()
    asyncio.run(run_report(runner.up(settings, database.name, url=db.url)))
    before = sha384(fix)
    fix.write_text('CREATE TABLE "missing" ("id" integer);\n')
    lines: list[str] = []
    events: list[runner.StepEvent | runner.RunNote] = []

    asyncio.run(
        runner.up(
            settings,
            database.name,
            url=db.url,
            progress=lines.append,
            on_event=events.append,
        )
    )

    assert said(events) == lines
    assert [type(event) for event in events] == [runner.RunNote, runner.StepEvent]
    assert events[0].text == (
        f"re-recorded 0002_fix/01_fix.up.sql (sha384:{before} → sha384:{sha384(fix)})"
    )


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_lock_wait_is_a_note_event(project, pkg, db):
    from tests.test_ddl_lock_timeout import ADD_SLUG, Holder, _postgres_only, _project

    _postgres_only(db)
    settings, database = await _project(project, pkg, db, 'ddl_lock_timeout = "1s"\n')
    sql_step(project, "add_slug", ADD_SLUG)
    lines: list[str] = []
    events: list[runner.StepEvent | runner.RunNote] = []

    with Holder(db, "author", seconds=1.5):
        await runner.up(
            settings,
            database.name,
            url=db.url,
            progress=lines.append,
            on_event=events.append,
        )

    assert said(events) == lines
    assert isinstance(events[0], runner.RunNote)
    assert events[0].text.startswith(
        '0003_add_slug  01_add_slug  waiting for a lock on "author" (attempt 1 of 10'
    )
    assert isinstance(events[-1], runner.StepEvent)
    assert events[-1].outcome == "applied"


def failing_second_step(project, pkg, db) -> None:
    """``0001`` with a second, hand-written step that fails."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    (folder,) = migrations(project).glob("0001_*")
    (folder / "02_two_b.up.sql").write_text('INSERT INTO "missing" VALUES (1);\n')
    (folder / "02_two_b.down.sql").write_text("SELECT 1;\n")


DATA_STEP_FAILS = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    raise RuntimeError("no team for this row")


@nothing_to_reverse("nothing was written")
def down(ctx): ...
"""


def failing_data_step(project, pkg, db) -> None:
    """``0001``, then ``0002`` whose one data step raises."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    new("backfill", "--data-only", "--data-step", "Author")
    (step,) = migrations(project).glob("0002_*/*.py")
    step.write_text(DATA_STEP_FAILS)


@pytest.mark.backend_matrix
def test_a_failed_step_is_an_event_progress_never_hears(project, pkg, db):
    failing_second_step(project, pkg, db)
    settings, database = settings_and_database()
    lines: list[str] = []
    events: list[runner.StepEvent | runner.RunNote] = []

    report = asyncio.run(
        run_report(
            runner.up(
                settings,
                database.name,
                url=db.url,
                progress=lines.append,
                on_event=events.append,
            )
        )
    )

    assert said(events) == lines
    assert len(lines) == 1 and lines[0].startswith("0001_create_author  01_schema  ")
    failed = events[-1]
    assert isinstance(failed, runner.StepEvent)
    assert (failed.migration, failed.file, failed.outcome, failed.reason) == (
        "0001_create_author",
        "02_two_b.up.sql",
        "failed",
        None,
    )
    assert failed.duration_ms == report.failed.duration_ms
    assert failed.line() is None


@pytest.mark.backend_matrix
def test_a_failed_sql_step_is_data_on_the_report(project, pkg, db):
    failing_second_step(project, pkg, db)
    settings, database = settings_and_database()

    report = asyncio.run(run_report(runner.up(settings, database.name, url=db.url)))

    failed = report.failed
    assert report.direction == "up"
    assert (failed.migration, failed.file, failed.rows_done) == (
        "0001_create_author",
        "02_two_b.up.sql",
        None,
    )
    assert "missing" in failed.error
    assert failed.hint == (
        "The step was rolled back; fix the file or the database and run `ferro "
        "migrate up` again to resume at it. Nothing after it ran."
    )
    assert report.refusal == (
        f"ferro migrate: 0001_create_author/02_two_b.up.sql failed: {failed.error}\n"
        f"{failed.hint}"
    )
    assert report.refused is None


@pytest.mark.backend_matrix
def test_a_failed_data_step_is_data_on_the_report(project, pkg, db):
    failing_data_step(project, pkg, db)
    settings, database = settings_and_database()

    report = asyncio.run(run_report(runner.up(settings, database.name, url=db.url)))

    failed = report.failed
    (step,) = migrations(project).glob("0002_*/*.py")
    assert (failed.migration, failed.file, failed.error) == (
        "0002_backfill",
        step.name,
        "RuntimeError: no team for this row",
    )
    assert report.refusal == (
        f"ferro migrate: 0002_backfill/{step.name} failed: {failed.error}\n"
        f"{failed.hint}"
    )
    assert failed.hint.endswith("Nothing after it ran.")


@pytest.mark.backend_matrix
def test_a_refused_run_names_its_direction_and_no_failed_step(project, pkg, db):
    two_migrations(project, pkg, db)
    settings, database = settings_and_database()
    asyncio.run(runner.up(settings, database.name, url=db.url))
    shutil.rmtree(migrations(project) / "0002_second")

    up = asyncio.run(run_report(runner.up(settings, database.name, url=db.url)))
    down = asyncio.run(
        run_report(runner.down(settings, database.name, url=db.url, target="0009"))
    )

    assert (up.direction, up.failed, up.refusal is not None) == ("up", None, True)
    assert (down.direction, down.failed, down.refusal is not None) == (
        "down",
        None,
        True,
    )


# -- the verbs: helpers -------------------------------------------------------------

OK = sgr("1;32", "✔")
APPLIED = sgr("32", "applied")
REVERTED = sgr("32", "reverted")
FAILED_GLYPH = sgr("1;31", "✘")
FAILED = sgr("1;31", "failed")
ROLLED_BACK = (
    "The step was rolled back; fix the file or the database and run `ferro migrate "
    "up` again to resume at it. Nothing after it ran."
)


def timed(text: str) -> str:
    """``text`` with every duration written ``N``: ``(N ms)`` in the plain
    form, ``N.Ns`` on a TTY."""
    return re.sub(r"\d+\.\ds", "N.Ns", re.sub(r"\(\d+ ms\)", "(N ms)", text))


def took() -> str:
    """A TTY row's duration, normalised by :func:`timed`."""
    return sgr("2", "  N.Ns")


DATA_STEP_PASSES = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    pass


@nothing_to_reverse("nothing was written")
def down(ctx): ...
"""


def with_a_data_step(project, pkg, db) -> None:
    """``0001_create_author`` (a schema step) and ``0002_backfill`` (one
    data step that writes nothing)."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    new("backfill", "--data-only", "--data-step", "Author")
    (step,) = migrations(project).glob("0002_*/*.py")
    step.write_text(DATA_STEP_PASSES)


def error_of(db, migration: int, step: int) -> str:
    [(error,)] = db.rows(
        f"SELECT error FROM _ferro_migrations WHERE migration = {migration} "
        f"AND step = {step}"
    )
    return error


AHEAD = (
    "ferro migrate: this database has applied 0002_second, which is not in "
    "migrations/.\nThe directory is behind the database: check out the branch "
    "that holds it. Nothing was applied."
)


# -- up -------------------------------------------------------------------------


@pytest.mark.backend_matrix
def test_plain_up_streams_todays_lines(project, pkg, db, capsys):
    two_migrations(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert timed(out) == (
        f"{db.line}\n"
        "0001_create_author  01_schema  applied (N ms)\n"
        "0002_second         01_second  applied (N ms)\n"
    )

    code, out, err = cli(capsys, "up", "--url", db.url)
    assert (code, out, err) == (
        0,
        f"{db.line}\nnothing to apply: the database is up to date\n",
        "",
    )


@pytest.mark.backend_matrix
def test_tty_up_rows_colour_each_step_by_kind(project, pkg, db, capsys, tty):
    with_a_data_step(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert timed(out) == (
        f"{tty_line(db)}\n"
        f"  {OK} 0001_create_author  {sgr('36', '01_schema')}{' ' * 11}"
        f"{APPLIED}{took()}\n"
        f"  {OK} 0002_backfill       {sgr('35', '01_backfill_author')}  "
        f"{APPLIED}{took()}\n"
    )

    code, out, err = cli(capsys, "up", "--url", db.url)
    assert (code, err) == (0, "")
    assert out == f"{tty_line(db)}\n" + tty_heading(
        "✔", "1;32", "nothing to apply", "the database is up to date"
    )


def adopted_then_pending(project, pkg, db) -> None:
    """A database that applied the originals of ``0002_epic``, the squash in
    their place, and ``0003_add_title`` pending above it."""
    squashed(project, pkg, db)
    write_models(project, pkg, TITLE)
    new("add_title")


@pytest.mark.backend_matrix
def test_plain_up_says_the_adoption_before_the_steps(project, pkg, db, capsys):
    adopted_then_pending(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert timed(out) == (
        f"{db.line}\n{ADOPTED}\n0003_add_title  01_schema  applied (N ms)\n"
    )


@pytest.mark.backend_matrix
def test_tty_up_heads_the_adoption_before_the_steps(project, pkg, db, capsys, tty):
    adopted_then_pending(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert timed(out) == (
        f"{tty_line(db)}\n"
        + tty_heading(
            "✔",
            "1;32",
            "adopted 0002_epic",
            "this database had applied 0002_add_tmp … 0004_drop_tmp; nothing was run",
        )
        + f"  {OK} 0003_add_title  {sgr('36', '01_schema')}  {APPLIED}{took()}\n"
    )


@pytest.mark.backend_matrix
def test_an_up_that_only_adopts_says_nothing_more_in_either_form(
    project, pkg, db, capsys, monkeypatch
):
    squashed(project, pkg, db)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert (code, err) == (0, "")
    assert out == f"{tty_line(db)}\n" + tty_heading(
        "✔",
        "1;32",
        "adopted 0002_epic",
        "this database had applied 0002_add_tmp … 0004_drop_tmp; nothing was run",
    )


@pytest.mark.backend_matrix
def test_plain_up_a_failed_step(project, pkg, db, capsys):
    failing_second_step(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert code == 1
    assert timed(out) == f"{db.line}\n0001_create_author  01_schema  applied (N ms)\n"
    assert err == (
        "ferro migrate: 0001_create_author/02_two_b.up.sql failed: "
        f"{error_of(db, 1, 2)}\n{ROLLED_BACK}\n"
    )


@pytest.mark.backend_matrix
def test_tty_up_a_failed_step_under_a_red_heading(project, pkg, db, capsys, tty):
    failing_second_step(project, pkg, db)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert code == 1
    assert timed(out) == (
        f"{tty_line(db)}\n"
        f"  {OK} 0001_create_author  {sgr('36', '01_schema')}  {APPLIED}{took()}\n"
        f"  {FAILED_GLYPH} 0001_create_author  {sgr('34', '02_two_b')}   "
        f"{FAILED}{took()}\n"
    )
    error = "".join(
        f"{sgr('31', f'  {line}')}\n" for line in error_of(db, 1, 2).splitlines()
    )
    assert err == (
        f"\n{sgr('1;31', '✘')} {sgr('1;31', '02_two_b failed')}\n"
        f"{error}  {ROLLED_BACK}\n"
    )


@pytest.mark.backend_matrix
def test_tty_up_a_failed_chunked_step_says_its_rows(project, pkg, db, capsys, tty):
    probe = project_with_authors(project, pkg, db)
    probe.reset()
    probe.fail_on = ("up", 2)

    code, out, err = cli(capsys, "up", "--url", db.url)

    assert code == 1
    assert timed(out).splitlines()[1:] == [
        f"  {OK} 0002_add_slug  {sgr('36', '01_schema')}           {APPLIED}" + took(),
        f"  {FAILED_GLYPH} 0002_add_slug  {sgr('35', '02_backfill_author')}  "
        f"{FAILED}{took()}",
    ]
    assert err == (
        f"\n{sgr('1;31', '✘')} {sgr('1;31', '02_backfill_author failed')}"
        f"{sgr('2', '  after 1,000 rows')}\n"
        f"{sgr('31', '  RuntimeError: batch 2 gave up')}\n"
        "  The failing batch was rolled back; the 1,000 rows of the batches before "
        "it stay committed. Fix the file or the database and run `ferro migrate "
        "up` again to resume after them. Nothing after it ran.\n"
    )


@pytest.mark.backend_matrix
def test_a_refused_up_in_both_forms(project, pkg, db, capsys, monkeypatch):
    two_migrations(project, pkg, db)
    assert run("migrate", "up", "--url", db.url) == 0
    shutil.rmtree(migrations(project) / "0002_second")

    code, out, err = cli(capsys, "up", "--url", db.url)
    assert (code, out, err) == (1, f"{db.line}\n", f"{AHEAD}\n")

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")
    code, out, err = cli(capsys, "up", "--url", db.url)
    assert (code, out) == (1, f"{tty_line(db)}\n")
    assert err == f"{sgr('1;31', '✘')} {sgr('1;31', 'up refused')}\n{AHEAD}\n"


@pytest.mark.backend_matrix
@pytest.mark.parametrize("form", ["plain", "tty"])
def test_up_streams_each_step_as_it_finishes(
    project, pkg, db, capsys, monkeypatch, form
):
    """A step that prints when it runs: the row of the step before it is
    already written, and the failure comes after it."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    new("backfill", "--data-only", "--data-step", "Author")
    (step,) = migrations(project).glob("0002_*/*.py")
    step.write_text(
        DATA_STEP_FAILS.replace(
            "    raise", '    print("<the second step runs>", flush=True)\n    raise'
        )
    )
    if form == "tty":
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("COLUMNS", "100")
    both = io.StringIO()
    monkeypatch.setattr(sys, "stdout", both)
    monkeypatch.setattr(sys, "stderr", both)

    assert run("migrate", "up", "--url", db.url) == 1

    said = both.getvalue()
    row = said.index("01_schema")
    running = said.index("<the second step runs>")
    failure = said.index("RuntimeError: no team for this row")
    assert row < running < failure


# -- down -----------------------------------------------------------------------


@pytest.mark.backend_matrix
def test_plain_down_prints_the_plan_then_each_step(project, pkg, db, capsys):
    two_migrations(project, pkg, db)
    assert run("migrate", "up", "--url", db.url) == 0

    code, out, err = cli(capsys, "down", "--yes", "--url", db.url)

    assert (code, err) == (0, "")
    assert timed(out) == (
        f"{db.line}\n"
        "down reverts, in this order:\n"
        "  0002_second  01_second\n"
        "0002_second  01_second  reverted (N ms)\n"
    )


@pytest.mark.backend_matrix
def test_tty_down_heads_the_plan_and_what_it_reverted(project, pkg, db, capsys, tty):
    with_a_data_step(project, pkg, db)
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0

    code, out, err = cli(capsys, "down", "--to", "0001", "--yes", "--url", db.url)

    assert (code, err) == (0, "")
    data = sgr("35", "01_backfill_author")
    assert timed(out) == (
        f"{tty_line(db)}\n"
        + tty_heading(
            "▲",
            "1;33",
            "down reverts, newest first",
            "0003_second, 0002_backfill, 2 steps",
        )
        + f"  0003_second    {sgr('34', '01_second')}\n"
        f"  0002_backfill  {data}{sgr('2', '  nothing to reverse: nothing was written')}\n"
        f"  {OK} 0003_second    {sgr('34', '01_second')}{' ' * 11}{REVERTED}{took()}\n"
        f"  {OK} 0002_backfill  {data}  {sgr('2', 'nothing to reverse')}"
        f"{sgr('2', ': nothing was written')}\n"
        + tty_heading(
            "✔",
            "1;32",
            "reverted 0003_second, 0002_backfill",
            "2 steps",
        )
    )


@pytest.mark.backend_matrix
def test_tty_down_without_a_terminal_refuses_under_a_heading(
    project, pkg, db, capsys, monkeypatch, tty
):
    from tests.test_migrate_down import Stdin

    two_migrations(project, pkg, db)
    assert run("migrate", "up", "--url", db.url) == 0
    monkeypatch.setattr(sys, "stdin", Stdin(tty=False))

    code, out, err = cli(capsys, "down", "--url", db.url)

    assert code == 1
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading("▲", "1;33", "down reverts, newest first", "0002_second, 1 step")
        + f"  0002_second  {sgr('34', '01_second')}\n"
    )
    assert err == (
        f"{sgr('1;31', '✘')} {sgr('1;31', 'down refused')}\n"
        "Not a terminal: pass --yes to revert without a prompt. Nothing was reverted.\n"
    )


# -- baseline -------------------------------------------------------------------


@pytest.mark.backend_matrix
def test_baseline_in_both_forms(project, pkg, db, capsys, monkeypatch):
    auto_migrated(project, pkg, db)
    data_step = migrations(project) / "0001_create_author/02_backfill_author.py"
    data_step.write_text(
        "@chunked(lambda models: models.Author.select(), batch_size=100)\n"
        "async def up(ctx, batch): ...\n\n\n"
        '@nothing_to_reverse("derived")\n'
        "def down(ctx): ...\n"
    )
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    code, out, err = cli(capsys, "baseline", "--url", db.url)

    assert (code, err) == (0, "")
    assert out == (
        f"{tty_line(db)}\n"
        + tty_heading(
            "✔", "1;32", f"recorded {BOTH} as baseline", "3 steps; 1 data step listed"
        )
        + f"  0001_create_author/{sgr('35', '02_backfill_author')}"
        f"{sgr('2', '.py')}{sgr('2', '  recorded, not run')}\n"
        f"{sgr('2', '  ' + NOT_COMPARED.rstrip())}\n"
    )

    monkeypatch.delenv("FORCE_COLOR")
    code, out, err = cli(capsys, "baseline", "--remove", "--url", db.url)
    assert (code, out, err) == (
        0,
        f"{db.line}\nremoved the baseline of {BOTH}\n",
        "",
    )


@pytest.mark.backend_matrix
def test_tty_baseline_that_finds_drift_heads_the_differences(
    project, pkg, db, capsys, tty
):
    auto_migrated(project, pkg, db)
    write_models(project, pkg, SQUADS)
    new("rename_team")

    code, out, err = cli(capsys, "baseline", "--url", db.url)

    lines = [
        "team table is named squad in the snapshot",
        "idx_team_size index is named idx_squad_size in the snapshot",
        "ck_team_size_ok check is named ck_squad_size_ok in the snapshot",
    ]
    if db.backend == "sqlite":
        lines += ["ck_squad_size_ok check is missing", "ck_team_size_ok check is extra"]
    report = asyncio.run(baseline(url=db.url))
    warned = sgr("1;33", "▲") + sgr("1;33", " warning")
    assert code == 4
    assert err == "".join(f"{warned}  {warning}\n" for warning in report.warnings)
    differences = f"{len(lines)} differences against 0003_rename_team"
    assert out == (
        f"{tty_line(db)}\n"
        f"{sgr('1;31', '✘')} {sgr('1;31', differences)}\n"
        + "".join(f"  {sgr('31', '•')} {line}\n" for line in lines)
        + f"{sgr('1', 'nothing was recorded')}\n"
    )


# -- rerecord -------------------------------------------------------------------


@pytest.mark.backend_matrix
def test_rerecord_continue_in_both_forms(project, pkg, db, capsys, monkeypatch):
    _, recorded, edited = edited_chunked(project, pkg, db)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "100")

    code, out, err = cli(capsys, "rerecord", "0002:02", "--continue", "--url", db.url)

    assert (code, err) == (0, "")
    assert out == (
        f"{tty_line(db)}\n"
        f"{OK} {sgr('1', 're-recorded 0002_add_slug/')}{sgr('1;35', '02_backfill_author')}"
        f"{sgr('1;2', '.py')}{sgr('2', '  nothing was run')}\n"
        f"  sha384:{recorded} {sgr('2', '→')} sha384:{edited}\n"
        f"{sgr('2', '  the next up continues from its cursor')}\n"
    )


@pytest.mark.parametrize(
    ("mode", "note"),
    [
        ("record", None),
        ("continue", "the next up continues from its cursor"),
        (
            "restart",
            "its cursor and rows_done are cleared, so the next up starts from the "
            "first row",
        ),
    ],
)
def test_a_rerecord_reports_what_it_leaves_the_next_up(mode, note):
    report = RerecordReport("0002_x/02_y.py", "a", "b", mode=mode)

    assert report.note == note
    tail = f"; {note}" if note else ""
    assert report.render() == (
        f"re-recorded 0002_x/02_y.py (sha384:a → sha384:b){tail}; nothing was run"
    )


# -- init -----------------------------------------------------------------------


def test_tty_init_dims_the_block_and_heads_what_it_wrote(project, capsys, tty):
    code, out, err = cli(
        capsys,
        "init",
        "--config-file",
        "ferro.toml",
        "--models",
        "app.models",
        "--dialects",
        "postgres",
    )

    assert (code, err) == (0, "")
    block = (project / "ferro.toml").read_text()
    assert out == (
        "".join(f"{sgr('2', line)}\n" if line else "\n" for line in block.splitlines())
        + tty_heading("✔", "1;32", "Wrote ferro.toml", "created migrations/")
    )
