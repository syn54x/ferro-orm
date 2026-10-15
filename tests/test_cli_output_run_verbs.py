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
import re

import pytest

from ferro.migrations import runner
from tests.test_cli_output_new_check import (  # noqa: F401 - fixtures
    no_bytecode,
    plain_env,
    tty,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
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
    assert [event.line() for event in events] == lines
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
    assert [event.line() for event in events] == lines
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

    assert [event.line() for event in events] == lines
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

    assert [event.line() for event in events] == lines
    assert isinstance(events[0], runner.RunNote)
    assert events[0].text.startswith(
        '0003_add_slug  01_add_slug  waiting for a lock on "author" (attempt 1 of 10'
    )
    assert isinstance(events[-1], runner.StepEvent)
    assert events[-1].outcome == "applied"
