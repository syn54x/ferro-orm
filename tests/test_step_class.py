# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Step classes, the tracking minor and ``allow_ahead="expand-only"`` (#626,
ADR-0054).

```python
await ferro.migrations.require_applied(allow_ahead="expand-only")
```

A rolling deploy's previous release boots beside a database that is ahead
of its checkout only by steps it can run beside. Every step's class is
recorded when it is applied, so a checkout that lacks the files still
answers "ahead by what?". Each test builds a real project under
``tmp_path``, applies its migrations with the newer checkout, then plays the
rolled-back release by deleting the newer migration's directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from ferro.migrations import DatabaseAheadError, runner
from ferro.migrations.rerecord import rerecord
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
    settings_and_database,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

# ferro 0.22's step record columns, the ones it names in every read and write.
COLUMNS_0_22 = (
    "migration, step, migration_name, file, kind, checksum, snapshot_checksum, "
    "started_at, finished_at, failed_at, error, resume_cursor, rows_done, "
    "duration_ms, ferro_version, origin, reverting, revert_cursor"
)

# A nullable column and a plain index: two expand steps.
BIO = AUTHOR + ("    bio: Annotated[str | None, FerroField(index=True)] = None\n")
# `status` dropped: a contract step.
WITHOUT_STATUS = AUTHOR.replace("    status: Status = Status.DRAFT\n", "")


def up(db, **kwargs) -> runner.RunReport:
    settings, database = settings_and_database()
    return asyncio.run(runner.up(settings, database.name, url=db.url, **kwargs))


def require_applied(db, **kwargs) -> None:
    settings, database = settings_and_database()
    return asyncio.run(
        runner.require_applied(settings, database.name, url=db.url, **kwargs)
    )


def status(db) -> runner.StatusReport:
    settings, database = settings_and_database()
    return asyncio.run(runner.status(settings, database.name, url=db.url))


def ahead_by(project: Path, pkg: str, db, name: str, models: str) -> str:
    """``0001_create_author`` and ``0002_<name>`` (``models``) applied, then
    ``0002``'s directory deleted, as the previous release's checkout lacks
    it. Returns ``0002_<name>``."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, models)
    new(name)
    assert run("migrate", "up", "--url", db.url) == 0
    shutil.rmtree(migrations(project) / f"0002_{name}")
    write_models(project, pkg, AUTHOR)
    return f"0002_{name}"


def classes(db) -> list[tuple[int, int, str | None]]:
    return [
        (row[0], row[1], row[2])
        for row in db.rows(
            "SELECT migration, step, class FROM _ferro_migrations "
            "ORDER BY migration, step"
        )
    ]


def columns(db, table: str) -> set[str]:
    if db.backend == "sqlite":
        return {row[1] for row in db.rows(f"PRAGMA table_info({table})")}
    return {
        row[0]
        for row in db.rows(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND table_name = '{table}'"
        )
    }


def as_0_22(db) -> None:
    """Reshape the tracking tables to what ferro 0.22 leaves: format 1, no
    ``minor`` column and no ``class`` column."""
    db.execute("ALTER TABLE _ferro_migrations DROP COLUMN class")
    db.execute("ALTER TABLE _ferro_migrations_format DROP COLUMN minor")


# -- allow_ahead="expand-only" ----------------------------------------------------------


def test_expand_only_admits_a_database_ahead_by_expand_steps(project, pkg, db):
    name = ahead_by(project, pkg, db, "bio", BIO)
    assert classes(db)[1:] == [(2, 1, "expand"), (2, 2, "expand")]

    assert require_applied(db, allow_ahead="expand-only") is None
    report = up(db, allow_ahead="expand-only")
    assert report.applied == [] and report.ahead == [name]


def test_expand_only_refuses_a_contract_step_naming_it(project, pkg, db):
    name = ahead_by(project, pkg, db, "drop", WITHOUT_STATUS)
    assert classes(db)[1] == (2, 1, "contract")

    with pytest.raises(DatabaseAheadError) as from_require:
        require_applied(db, allow_ahead="expand-only")
    with pytest.raises(DatabaseAheadError) as from_up:
        up(db, allow_ahead="expand-only")
    for raised in (from_require, from_up):
        assert raised.value.ahead == [name]
        assert raised.value.contract_steps == ["0002_drop:01"]
        message = str(raised.value)
        assert "0002_drop:01 (contract" in message, message
        assert "Pass allow_ahead=True to run beside them" in message

    # `True` is the unbounded form; `False` refuses any distance.
    assert require_applied(db, allow_ahead=True) is None
    with pytest.raises(DatabaseAheadError) as plain:
        require_applied(db)
    assert plain.value.contract_steps == ["0002_drop:01"]


def test_the_cli_flag_maps_to_the_same_values(project, pkg, db, capsys):
    ahead_by(project, pkg, db, "drop", WITHOUT_STATUS)
    capsys.readouterr()

    assert run("migrate", "up", "--allow-ahead=expand-only", "--url", db.url) == 1
    assert "0002_drop:01 (contract" in capsys.readouterr().err
    for flag in ("--allow-ahead", "--allow-ahead=true"):
        assert run("migrate", "up", flag, "--url", db.url) == 0, flag
    assert run("migrate", "up", "--allow-ahead=false", "--url", db.url) == 1
    assert run("migrate", "up", "--allow-ahead=sometimes", "--url", db.url) != 0


def test_the_cli_flag_admits_an_expand_step(project, pkg, db, capsys):
    ahead_by(project, pkg, db, "bio", BIO)
    assert run("migrate", "up", "--allow-ahead=expand-only", "--url", db.url) == 0
    assert run("migrate", "up", "--url", db.url) == 1


def test_any_other_allow_ahead_is_a_type_error_naming_the_three(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    for call in (up, require_applied):
        with pytest.raises(TypeError) as raised:
            call(db, allow_ahead="anything-else")
        text = str(raised.value)
        assert "False" in text and "True" in text and '"expand-only"' in text
    assert "_ferro_migrations" not in db.tables(), "refused before connecting"


# -- hand-written SQL steps -------------------------------------------------------------


def test_a_hand_sql_step_is_admitted_only_when_it_declares_expand(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(
        project, "notes", '-- ferro: expand\nCREATE TABLE "notes" ("id" integer);\n'
    )
    sql_step(project, "tags", 'CREATE TABLE "tags" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0
    assert classes(db) == [(1, 1, "expand"), (2, 1, "expand"), (3, 1, "contract")]

    shutil.rmtree(migrations(project) / "0003_tags")
    with pytest.raises(DatabaseAheadError) as raised:
        require_applied(db, allow_ahead="expand-only")
    assert raised.value.contract_steps == ["0003_tags:01"]

    shutil.rmtree(migrations(project) / "0002_notes")
    db.execute("DELETE FROM _ferro_migrations WHERE migration = 3")
    assert require_applied(db, allow_ahead="expand-only") is None


def test_a_generated_step_edited_to_drop_a_column_is_recorded_contract(
    project, pkg, db
):
    # Panel F1: ADR-0030 lets a person edit a step no database finished. The
    # class is what the file does, so a drop added by hand to a generated
    # expand step is contract, whatever the generator would have said.
    nickname = AUTHOR + "    nickname: str | None = None\n"
    configure(project, pkg, db.backend)
    write_models(project, pkg, nickname)
    new("create_author")
    write_models(project, pkg, nickname + "    bio: str | None = None\n")
    new("bio")
    step = migrations(project) / f"0002_bio/01_schema.up.{db.backend}.sql"
    step.write_text(step.read_text() + 'ALTER TABLE "author" DROP COLUMN "nickname";\n')

    assert up(db).applied
    assert classes(db) == [(1, 1, "expand"), (2, 1, "contract")]
    assert "nickname" not in columns(db, "author")


def test_the_expand_header_takes_no_text(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "notes", '-- ferro: expand now\nCREATE TABLE "n" ("id" int);\n')
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1
    assert "-- ferro: expand now" in capsys.readouterr().err
    assert "_ferro_migrations" not in db.tables()


# -- the tracking minor -----------------------------------------------------------------


def test_a_minor_0_database_reads_as_contract_and_the_next_up_classifies_it(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("bio")
    assert run("migrate", "up", "--url", db.url) == 0
    as_0_22(db)
    shape = (columns(db, "_ferro_migrations"), columns(db, "_ferro_migrations_format"))
    capsys.readouterr()

    # A reader treats a missing class as contract and writes nothing.
    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    out = capsys.readouterr().out
    steps = [line for line in out.splitlines() if line.startswith("      ")]
    assert len(steps) == 3 and all(line.endswith("  contract") for line in steps), out
    assert require_applied(db) is None
    assert (
        columns(db, "_ferro_migrations"),
        columns(db, "_ferro_migrations_format"),
    ) == shape

    # The next mutating verb adds the column under the run lock, records the
    # minor and classifies every record from the files; format stays 1.
    assert up(db).applied == []
    assert db.rows("SELECT format, minor FROM _ferro_migrations_format") == [(1, 1)]
    assert classes(db) == [(1, 1, "expand"), (2, 1, "expand"), (2, 2, "expand")]
    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    # The database line names the SQLite file, after this test.
    assert "contract" not in capsys.readouterr().out.split("\n", 1)[1]

    # A second run finds nothing to classify.
    assert up(db).applied == []
    assert classes(db) == [(1, 1, "expand"), (2, 1, "expand"), (2, 2, "expand")]


def test_records_of_a_migration_the_directory_lacks_are_classified_contract(
    project, pkg, db
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("bio")
    assert run("migrate", "up", "--url", db.url) == 0
    as_0_22(db)
    shutil.rmtree(migrations(project) / "0002_bio")

    assert up(db, allow_ahead=True).ahead == ["0002_bio"]
    assert classes(db) == [(1, 1, "expand"), (2, 1, "contract"), (2, 2, "contract")]
    assert db.rows("SELECT format FROM _ferro_migrations_format") == [(1,)]
    with pytest.raises(DatabaseAheadError):
        require_applied(db, allow_ahead="expand-only")


def test_a_record_written_with_0_22_columns_is_contract_until_the_next_up(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("bio")
    assert run("migrate", "up", "--url", db.url) == 0
    # A rolled-back 0.22 runner re-writes 0002's first record naming only
    # the columns it knows.
    db.execute(
        f"CREATE TABLE saved AS SELECT {COLUMNS_0_22} FROM _ferro_migrations "
        "WHERE migration = 2 AND step = 1"
    )
    db.execute("DELETE FROM _ferro_migrations WHERE migration = 2 AND step = 1")
    db.execute(
        f"INSERT INTO _ferro_migrations ({COLUMNS_0_22}) "
        f"SELECT {COLUMNS_0_22} FROM saved"
    )
    db.execute("DROP TABLE saved")
    assert classes(db)[1] == (2, 1, None)
    capsys.readouterr()

    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    out = capsys.readouterr().out
    shown = [line.split() for line in out.splitlines() if line.startswith("      ")]
    assert shown[1] == [f"01_schema.up.{db.backend}.sql", "applied", "contract"], out
    assert require_applied(db) is None
    assert classes(db)[1] == (2, 1, None), "readers never write"

    assert up(db).applied == []
    assert classes(db)[1] == (2, 1, "expand")


def test_a_record_whose_migration_was_replaced_in_place_is_classified_contract(
    project, pkg, db
):
    # Panel F3: 0002_change applied a drop (contract) under 0.22, then was
    # replaced in place by an expand migration of the same number and name.
    # The files on disk are not the ones the record applied: the backfill
    # records contract, never the new SQL's expand, and the verb then refuses.
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, WITHOUT_STATUS)
    new("change")
    assert run("migrate", "up", "--url", db.url) == 0
    as_0_22(db)
    write_models(project, pkg, BIO)
    new("change", "--replace", "0002_change")
    assert (migrations(project) / "0002_change").is_dir()

    with pytest.raises(runner.MigrationRefused) as raised:
        up(db)
    assert raised.value.report.refused.kind == "snapshot_mismatch"
    assert classes(db) == [(1, 1, "expand"), (2, 1, "contract")]


def test_a_migration_that_does_not_regenerate_refuses_up_and_warns_down(
    project, pkg, db
):
    # Panel F2: 0002_bio's records were written by 0.22 (no class), and its
    # snapshot now moves author's key, which the generator refuses. `up`
    # decides classes and refuses before writing anything; `rerecord` and
    # `down` read none, so they run, leaving the class NULL with a warning.
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, AUTHOR + "    bio: str | None = None\n")
    new("bio")
    assert run("migrate", "up", "--url", db.url) == 0
    ir = migrations(project) / "0002_bio/ir.json"
    snapshot = json.loads(ir.read_text())
    (author,) = snapshot["payload"]["models"]
    for column in author["columns"]:
        column["primary_key"] = column["name"] == "name"
        column["autoincrement"] = False
    ir.write_text(json.dumps(snapshot, indent=2) + "\n")
    checksum = hashlib.sha384(ir.read_bytes()).hexdigest()
    db.execute(
        f"UPDATE _ferro_migrations SET class = NULL, snapshot_checksum = '{checksum}' "
        "WHERE migration = 2"
    )
    tracking = db.rows("SELECT * FROM _ferro_migrations ORDER BY migration, step")

    with pytest.raises(runner.MigrationRefused) as raised:
        up(db)
    assert raised.value.report.refused.kind == "unclassifiable"
    assert str(raised.value).startswith(
        "ferro migrate: 0002_bio does not regenerate from its snapshots, so ferro "
        "cannot decide the class of its generated steps (ADR-0054): "
    ), raised.value
    assert str(raised.value).endswith(
        "No class was recorded for it, and none of its steps was applied."
    )
    assert db.rows("SELECT * FROM _ferro_migrations ORDER BY migration, step") == (
        tracking
    )

    step = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    step.write_text(step.read_text() + "-- reviewed\n")
    settings, database = settings_and_database()
    with pytest.warns(UserWarning, match="0002_bio does not regenerate") as rerecorded:
        report = asyncio.run(
            rerecord(settings, database.name, url=db.url, target="0001:01")
        )
    assert report.notes == [str(w.message) for w in rerecorded]
    assert report.notes[0].endswith(
        "The next `ferro migrate up` or `ferro migrate baseline` classifies them, "
        "or refuses naming the fix."
    ), report.notes
    assert classes(db) == [(1, 1, "expand"), (2, 1, None)]

    with pytest.warns(UserWarning, match="0002_bio does not regenerate"):
        reverted = asyncio.run(
            runner.down(settings, database.name, url=db.url, confirm=None)
        )
    assert [step.migration for step in reverted.reverted] == ["0002_bio"]
    assert reverted.notes and "0002_bio" in reverted.notes[0]
    assert classes(db) == [(1, 1, "expand")]


def test_status_on_a_lower_minor_writes_nothing(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    as_0_22(db)

    report = status(db)
    assert [s.step_class for m in report.migrations for s in m.steps] == ["contract"]
    assert "minor" not in columns(db, "_ferro_migrations_format")
    assert "class" not in columns(db, "_ferro_migrations")


def test_generated_files_carry_no_class_header(project, pkg, db):
    # The class is the planner's, recorded at apply time: a header would
    # change every applied file's checksum (ADR-0030).
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, WITHOUT_STATUS)
    new("drop")
    files = sorted(migrations(project).rglob("*.sql"))
    assert files
    for path in files:
        assert "-- ferro: expand" not in path.read_text(), path


# -- the deploying guide ----------------------------------------------------------------

DEPLOYING = Path(__file__).resolve().parents[1] / "docs/pages/guide/schema/deploying.md"


def test_the_deploying_guide_quotes_the_refusal_expand_only_raises(project, pkg, db):
    """The rolling-deploy recipe is ``"expand-only"``, and the refusal it
    quotes is the text ferro raises (the guide's migration is ``0008``)."""
    page = DEPLOYING.read_text()
    recipe = 'await ferro.migrations.require_applied(allow_ahead="expand-only")'
    assert re.search(rf"```python[^\n]*\n{re.escape(recipe)}\n```", page)
    quoted = re.search(
        r"```text\n(ferro\.migrations\.DatabaseAheadError: [^`]*?0008_drop_bio:01[^`]*)```",
        page,
    )
    assert quoted is not None

    configure(project, pkg, db.backend)
    write_models(project, pkg, BIO)
    new("create_author")
    write_models(project, pkg, AUTHOR)
    new("drop_bio")
    assert run("migrate", "up", "--url", db.url) == 0
    shutil.rmtree(migrations(project) / "0002_drop_bio")
    with pytest.raises(DatabaseAheadError) as raised:
        require_applied(db, allow_ahead="expand-only")
    assert raised.value.contract_steps == ["0002_drop_bio:01"]
    raised_text = f"ferro.migrations.DatabaseAheadError: {raised.value}\n".replace(
        "0002_drop_bio", "0008_drop_bio"
    )
    assert quoted.group(1) == raised_text
