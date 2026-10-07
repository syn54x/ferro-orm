# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for edits to an existing table (#524).

```text
$ ferro migrate new author_bio
  0002_author_bio/
    01_schema.up.postgres.sql     ALTER TABLE "author" ADD COLUMN "bio" varchar;
    01_schema.down.postgres.sql   ALTER TABLE "author" DROP COLUMN "bio";
    01_schema.up.sqlite.sql       ALTER TABLE "author" ADD COLUMN "bio" varchar;
    01_schema.down.sqlite.sql     ALTER TABLE "author" DROP COLUMN "bio";
    ir.json
```

Every round trip builds a real project under ``tmp_path``, applies the
parent migration with ``ferro migrate up`` against the parametrized database
(SQLite and Postgres), generates the edit, and checks: the up file holds
exactly the statements the reconciliation pass plans from the live schema to
the edited models (AGENTS.md § I-1); ``up`` applies; the live schema has no
drift against the new snapshot; ``down`` reverts; no drift against the
parent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

import ferro
from ferro import _core
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
    tables_of,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    isolated_imports,
    listing,
    pkg,
    project,
    run,
    statements,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: an edit that keeps ``models.py``'s size (``int`` →
    ``str``) inside the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


backend_matrix = pytest.mark.backend_matrix


# -- helpers --------------------------------------------------------------------------


def pass_statements(db, target: dict, parent: dict) -> list[str]:
    """What the reconciliation pass would execute to bring the live database
    to ``target``: the one planner from the live schema, rendered."""

    async def read() -> list[str]:
        name = f"parity_{uuid.uuid4().hex}"
        await ferro.connect(db.url, name=name)
        try:
            live, facts = await _core._live_schema_ir(
                name, json.dumps(sorted(tables_of(parent) | tables_of(target)))
            )
        finally:
            await _core._disconnect(name)
        plan = json.loads(
            _core._plan_from_ir(
                live,
                json.dumps(target),
                db.backend,
                '{"destructive": true}',
                render=True,
                facts_json=facts,
            )
        )
        return [sql for op in plan["operations"] for sql in op["statements"]]

    return asyncio.run(read())


def schema_file(project: Path, number: int, direction: str, backend: str) -> Path:
    return migration_dir(project, number) / f"01_schema.{direction}.{backend}.sql"


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models and is applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0


def edit(
    project: Path, pkg: str, db, body: str, name: str, validates: bool = False
) -> tuple[Path, Path]:
    """Generate ``0002`` for ``body`` and pin its up file to the pass's
    statements over the live schema. Returns its up and down files.

    On Postgres a foreign key or check added to the existing table is staged
    (#527): added ``NOT VALID`` (the pass's statement plus that one token) and
    validated by a ``02_validate`` step, which ``validates`` expects."""
    write_models(project, pkg, body)
    new(name)
    validate = (
        [f"02_validate.down.{db.backend}.sql", f"02_validate.up.{db.backend}.sql"]
        if validates and db.backend == "postgres"
        else []
    )
    assert sorted(p.name for p in migration_dir(project, 2).iterdir()) == [
        f"01_schema.down.{db.backend}.sql",
        f"01_schema.up.{db.backend}.sql",
        *validate,
        "ir.json",
    ]
    up = schema_file(project, 2, "up", db.backend)
    assert [sql.replace(" NOT VALID", "") for sql in statements(up)] == (
        pass_statements(db, snapshot(project, 2), snapshot(project, 1))
    )
    return up, schema_file(project, 2, "down", db.backend)


def round_trip(project: Path, db) -> None:
    """``up`` applies ``0002`` with no drift; ``down`` reverts it with no
    drift against the parent; ``up`` applies it again."""
    assert run("migrate", "up", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 2), snapshot(project, 1)) == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db)[:2] == [(1, 1), (2, 1)]


def refused(project: Path, pkg: str, dialect: str, before: str, after: str) -> str:
    """Generate ``before``, then ``after``; returns what ``new`` refused
    with, after checking it wrote nothing and exited 1."""
    write_config(project, pkg, f'["{dialect}"]')
    write_models(project, pkg, before)
    new("create")
    write_models(project, pkg, after)
    written = listing(project / "migrations")
    return_code, err = _new_capturing("edit")
    assert return_code == 1
    assert listing(project / "migrations") == written
    return err


def _new_capturing(name: str) -> tuple[int, str]:
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = run("migrate", "new", name)
    return code, err.getvalue()


BIO = AUTHOR + "    bio: str | None = None\n"


# -- the transcript, offline ----------------------------------------------------------


def test_an_optional_column_writes_both_dialects_with_a_drop_for_its_down(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    assert run("migrate", "new", "create_author") == 0
    write_models(project, pkg, BIO)
    capsys.readouterr()

    assert run("migrate", "new", "author_bio") == 0

    migration = project / "migrations/0002_author_bio"
    assert listing(migration) == [
        "01_schema.down.postgres.sql",
        "01_schema.down.sqlite.sql",
        "01_schema.up.postgres.sql",
        "01_schema.up.sqlite.sql",
        "ir.json",
    ]
    for dialect in ("postgres", "sqlite"):
        assert (migration / f"01_schema.up.{dialect}.sql").read_text() == (
            'ALTER TABLE "author" ADD COLUMN "bio" varchar;\n'
        )
        # The down drops what the up added; a down is never destructive.
        assert (migration / f"01_schema.down.{dialect}.sql").read_text() == (
            'ALTER TABLE "author" DROP COLUMN "bio";\n'
        )
    assert "changed models: Author" in capsys.readouterr().out


# -- A1, A2: adding a column ----------------------------------------------------------


@backend_matrix
def test_a1_an_optional_column_round_trips(project, pkg, db):
    start(project, pkg, db, AUTHOR)

    up, down = edit(project, pkg, db, BIO, "author_bio")

    assert up.read_text() == 'ALTER TABLE "author" ADD COLUMN "bio" varchar;\n'
    assert down.read_text() == 'ALTER TABLE "author" DROP COLUMN "bio";\n'
    round_trip(project, db)


MOOD = 'class Mood(StrEnum):\n    CALM = "calm"\n    LOUD = "loud"\n\n\n'


@backend_matrix
def test_a1_an_optional_enum_column_round_trips(project, pkg, db):
    start(project, pkg, db, AUTHOR)
    body = MOOD + AUTHOR + "    mood: Mood | None = None\n"

    up, down = edit(project, pkg, db, body, "author_mood")

    sql = statements(up)
    if db.backend == "postgres":
        assert 'CREATE TYPE "mood"' in sql[0]
        assert statements(down) == [
            'ALTER TABLE "author" DROP COLUMN "mood"',
            'DROP TYPE "mood"',
        ]
    else:
        assert sql == ['ALTER TABLE "author" ADD COLUMN "mood" varchar(4)']
        assert statements(down) == ['ALTER TABLE "author" DROP COLUMN "mood"']
    round_trip(project, db)


@backend_matrix
def test_a1_an_optional_checked_column_carries_its_check(project, pkg, db):
    start(project, pkg, db, AUTHOR)
    body = (
        MOOD
        + AUTHOR
        + '    mood: Annotated[Mood | None, FerroField(db_type="text", db_check=True)]'
        + " = None\n"
    )

    up, down = edit(project, pkg, db, body, "author_mood", validates=True)

    sql = statements(up)
    check = "CHECK (\"mood\" IN ('calm', 'loud'))"
    if db.backend == "sqlite":
        # Inline on SQLite's ADD COLUMN (#514); dropped with the column.
        assert sql == [
            f'ALTER TABLE "author" ADD COLUMN "mood" text CONSTRAINT "ck_author_mood" {check}'
        ]
        assert statements(down) == ['ALTER TABLE "author" DROP COLUMN "mood"']
    else:
        assert sql[0] == 'ALTER TABLE "author" ADD COLUMN "mood" text'
        assert f"{check} NOT VALID;" in sql[1]
        assert statements(down) == [
            'ALTER TABLE "author" DROP CONSTRAINT "ck_author_mood"',
            'ALTER TABLE "author" DROP COLUMN "mood"',
        ]
    round_trip(project, db)


TEAM = """
class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""


@backend_matrix
def test_a1_an_optional_foreign_key_round_trips(project, pkg, db):
    start(project, pkg, db, TEAM + AUTHOR)
    body = (
        TEAM
        + '    members: Relation[list["Author"]] = BackRef()\n'
        + AUTHOR
        + '    team: Annotated[Team | None, ForeignKey(related_name="members", '
        'on_delete="SET NULL")] = None\n'
    )

    up, down = edit(project, pkg, db, body, "author_team", validates=True)

    sql = statements(up)
    if db.backend == "sqlite":
        # SQLite's ADD COLUMN carries the constraint inline (#514).
        assert sql == [
            'ALTER TABLE "author" ADD COLUMN "team_id" integer '
            'REFERENCES "team"("id") ON DELETE SET NULL'
        ]
        # SQLite cannot drop a column a table-level FOREIGN KEY names, the
        # shape any rebuild of the table writes: the down is a rebuild (#526).
        assert down.read_text().startswith("-- ferro: foreign-keys-off\n")
        assert statements(down)[0].startswith(
            'CREATE TABLE IF NOT EXISTS "_ferro_new_author"'
        )
    else:
        # Staged NOT VALID and validated by 02_validate (#527).
        assert sql[-1] == (
            'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_team" '
            'FOREIGN KEY ("team_id") REFERENCES "team" ("id") ON DELETE SET NULL '
            "NOT VALID"
        )
        assert statements(down) == ['ALTER TABLE "author" DROP COLUMN "team_id"']
    round_trip(project, db)


@backend_matrix
def test_a2_a_required_column_with_a_literal_default_backfills_existing_rows(
    project, pkg, db
):
    start(project, pkg, db, AUTHOR)
    db.execute("INSERT INTO author (name, status) VALUES ('ada', 'draft')")

    up, down = edit(project, pkg, db, AUTHOR + '    tier: str = "free"\n', "tier")

    sql = statements(up)
    assert sql[0] == (
        'ALTER TABLE "author" ADD COLUMN "tier" varchar NOT NULL DEFAULT \'free\''
    )
    if db.backend == "postgres":
        assert sql[1:] == ['ALTER TABLE "author" ALTER COLUMN "tier" DROP DEFAULT']
    else:
        assert sql[1:] == [], "SQLite has no DROP DEFAULT: it lingers, as in the pass"
    assert not up.read_text().startswith("-- ferro:")
    assert down.read_text() == 'ALTER TABLE "author" DROP COLUMN "tier";\n'
    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows("SELECT tier FROM author") == [("free",)]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []
    assert run("migrate", "up", "--url", db.url) == 0
    assert plan_against(db, snapshot(project, 2), snapshot(project, 1)) == []


# -- A4: dropping a column ----------------------------------------------------------------


@backend_matrix
def test_a4_dropping_an_optional_column_is_destructive_and_its_down_adds_it_back(
    project, pkg, db
):
    start(project, pkg, db, BIO)

    up, down = edit(project, pkg, db, AUTHOR, "drop_bio")

    assert up.read_text() == (
        '-- ferro: destructive\n\nALTER TABLE "author" DROP COLUMN "bio";\n'
    )
    # Put back exactly as the parent declared it, nullable: no data-dependent.
    assert down.read_text() == 'ALTER TABLE "author" ADD COLUMN "bio" varchar;\n'
    round_trip(project, db)


NICKNAME = AUTHOR + "    nickname: str\n"


@pytest.mark.postgres_only
def test_a4_a_dropped_not_null_column_comes_back_not_null_or_the_down_fails(
    project, pkg, db, capsys
):
    start(project, pkg, db, NICKNAME)
    db.execute(
        "INSERT INTO author (name, status, nickname) VALUES ('ada', 'draft', 'a')"
    )

    up, down = edit(project, pkg, db, AUTHOR, "drop_nickname")

    assert up.read_text().startswith("-- ferro: destructive\n")
    # As declared, NOT NULL: never a relaxed schema (ADR-0033).
    assert down.read_text() == (
        "-- ferro: data-dependent\n\n"
        'ALTER TABLE "author" ADD COLUMN "nickname" varchar;\n\n'
        'ALTER TABLE "author" ALTER COLUMN "nickname" SET NOT NULL;\n'
    )
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    # Populated: the down fails (23502, NotNullViolationError) and stands.
    assert run("migrate", "down", "--yes", "--url", db.url) == 1
    assert "contains null values" in capsys.readouterr().err
    record = db.records()[1]
    assert record[10] is None and record[11] is None, "rolled back: unchanged"
    assert "nickname" not in [
        row[0]
        for row in db.rows(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND table_name = 'author'"
        )
    ], "the failed down rolled back"

    # Empty: it reaches the parent snapshot.
    db.execute("DELETE FROM author")
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert plan_against(db, snapshot(project, 1), snapshot(project, 2)) == []


def generated_sqlite_files(project: Path, pkg: str, before: str, after: str):
    """Generate ``before``, then ``after``, for SQLite alone; returns
    ``0002``'s up and down file texts."""
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, before)
    new("create")
    write_models(project, pkg, after)
    new("edit")
    return (
        schema_file(project, 2, "up", "sqlite").read_text(),
        schema_file(project, 2, "down", "sqlite").read_text(),
    )


REBUILD = "-- ferro: foreign-keys-off\n"


def test_a4_a_dropped_not_null_column_on_sqlite_comes_back_by_a_rebuild(project, pkg):
    # Round trips: tests/test_generate_sqlite_rebuild.py (#526).
    up, down = generated_sqlite_files(project, pkg, NICKNAME, AUTHOR)
    assert up.startswith("-- ferro: destructive\n")
    assert down.startswith(REBUILD + "-- ferro: data-dependent\n")


# -- A6, A7b: a column's type and nullability on Postgres ---------------------------------


@pytest.mark.postgres_only
def test_a6_a_type_change_casts_both_ways_marked_data_dependent(project, pkg, db):
    start(project, pkg, db, AUTHOR + "    age: int | None = None\n")

    up, down = edit(project, pkg, db, AUTHOR + "    age: str | None = None\n", "age")

    assert up.read_text() == (
        "-- ferro: data-dependent\n\n"
        'ALTER TABLE "author" ALTER COLUMN "age" TYPE varchar USING "age"::varchar;\n'
    )
    assert down.read_text() == (
        "-- ferro: data-dependent\n\n"
        'ALTER TABLE "author" ALTER COLUMN "age" TYPE integer USING "age"::integer;\n'
    )
    round_trip(project, db)


@pytest.mark.postgres_only
def test_a6_rows_that_cannot_cast_fail_the_step_and_record_it(project, pkg, db, capsys):
    start(project, pkg, db, AUTHOR + "    code: str | None = None\n")
    db.execute("INSERT INTO author (name, status, code) VALUES ('ada', 'draft', 'abc')")

    up, _ = edit(project, pkg, db, AUTHOR + "    code: int | None = None\n", "code")
    assert up.read_text().startswith("-- ferro: data-dependent\n")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    # 22P02, a data exception: the step is named and its record failed.
    err = capsys.readouterr().err
    assert "ferro migrate: 0002_code/01_schema.up.postgres.sql failed:" in err
    assert 'invalid input syntax for type integer: "abc"' in err
    record = db.records()[1]
    assert record[9] is None and record[10] is not None
    assert "invalid input syntax" in record[11]
    assert db.rows("SELECT code FROM author") == [("abc",)]


@pytest.mark.postgres_only
def test_a7b_relaxing_not_null_and_its_down_sets_it_again(project, pkg, db):
    start(project, pkg, db, NICKNAME)

    up, down = edit(
        project, pkg, db, AUTHOR + "    nickname: str | None = None\n", "relax"
    )

    assert up.read_text() == (
        'ALTER TABLE "author" ALTER COLUMN "nickname" DROP NOT NULL;\n'
    )
    assert down.read_text() == (
        "-- ferro: data-dependent\n\n"
        'ALTER TABLE "author" ALTER COLUMN "nickname" SET NOT NULL;\n'
    )
    round_trip(project, db)


# -- A9: indexes ---------------------------------------------------------------------------
# An index on an existing table is its own index step on every dialect:
# tests/test_generate_postgres_staging.py (#527).

TAG = """
class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: Annotated[str, FerroField(unique=True)]
"""


@pytest.mark.postgres_only
def test_a9_a_unique_on_a_table_the_migration_creates_is_inline_on_postgres(
    project, pkg, db
):
    start(project, pkg, db, AUTHOR)

    up, down = edit(project, pkg, db, AUTHOR + TAG, "tag")

    # The table's own step carries its unique: no index step, nothing refused.
    sql = statements(up)
    assert sql[0].startswith('CREATE TABLE IF NOT EXISTS "tag"')
    assert sql[1:] == [
        'CREATE UNIQUE INDEX IF NOT EXISTS "uq_tag_label" ON "tag" ("label")'
    ]
    assert statements(down) == ['DROP TABLE "tag"']
    round_trip(project, db)


# -- A8, C5: no schema change --------------------------------------------------------------


def test_a8_a_default_change_on_an_existing_column_is_no_schema_change(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR + '    tier: str = "free"\n')
    assert run("migrate", "new", "first") == 0
    before = listing(project / "migrations")
    write_models(project, pkg, AUTHOR + '    tier: str = "pro"\n')
    capsys.readouterr()

    assert run("migrate", "new", "nothing") == 0

    assert capsys.readouterr().out == "no schema change: nothing written\n"
    assert listing(project / "migrations") == before


# -- F1: several models in one edit -------------------------------------------------------


@backend_matrix
def test_f1_several_models_edited_at_once_share_one_step_parents_first(
    project, pkg, db
):
    start(project, pkg, db, LIBRARY)
    body = (
        LIBRARY.replace(
            "    status: Status = Status.DRAFT\n",
            "    status: Status = Status.DRAFT\n    bio: str | None = None\n",
        )
        + "    subtitle: str | None = None\n"
    )

    up, down = edit(project, pkg, db, body, "bio_and_subtitle")

    assert statements(up) == [
        'ALTER TABLE "author" ADD COLUMN "bio" varchar',
        'ALTER TABLE "post" ADD COLUMN "subtitle" varchar',
    ]
    assert statements(down) == [
        'ALTER TABLE "author" DROP COLUMN "bio"',
        'ALTER TABLE "post" DROP COLUMN "subtitle"',
    ]
    round_trip(project, db)


# -- refusals ------------------------------------------------------------------------------


def test_a6_on_sqlite_is_a_rebuild(project, pkg):
    # Round trips: tests/test_generate_sqlite_rebuild.py (#526).
    up, down = generated_sqlite_files(
        project,
        pkg,
        AUTHOR + "    age: int | None = None\n",
        AUTHOR + "    age: str | None = None\n",
    )
    assert up.startswith(REBUILD + "-- ferro: data-dependent\n")
    assert "WHERE typeof(\"age\") NOT IN ('text', 'null')" in up
    assert "CAST(" not in up
    assert down.startswith(REBUILD + "-- ferro: data-dependent\n")


def test_a7b_on_sqlite_is_a_rebuild(project, pkg):
    # Round trips: tests/test_generate_sqlite_rebuild.py (#526).
    up, down = generated_sqlite_files(
        project, pkg, NICKNAME, AUTHOR + "    nickname: str | None = None\n"
    )
    assert up.startswith(REBUILD + "\n")
    assert down.startswith(REBUILD + "-- ferro: data-dependent\n")


def test_a3_a_required_column_without_a_default_is_refused_until_the_backfill(
    project, pkg
):
    err = refused(project, pkg, "postgres", AUTHOR, AUTHOR + "    slug: str\n")
    assert err == (
        "not generated yet: AddColumn on author needs a backfill (ticket #534)\n"
    )


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
def test_a_primary_key_change_is_refused_with_the_recipe(project, pkg, dialect):
    moved = AUTHOR.replace(
        "    id: Annotated[int | None, FerroField(primary_key=True)] = None\n"
        "    name: Annotated[str, FerroField(unique=True)]\n",
        "    id: int | None = None\n"
        "    name: Annotated[str, FerroField(primary_key=True)]\n",
    )
    assert moved != AUTHOR
    err = refused(project, pkg, dialect, AUTHOR, moved)
    assert err == (
        "not generated yet: a primary-key change on author (ticket "
        "#536): a table's primary key cannot change in place; declare a new model "
        "with the new key, copy the rows across, then drop the old model\n"
    )
