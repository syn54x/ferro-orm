# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for changes SQLite's ``ALTER TABLE`` cannot express (#526).

```text
$ ferro migrate new author_age_text
  0002_author_age_text/
    01_schema.up.postgres.sql   -- ferro: data-dependent
                                ALTER TABLE "author" ALTER COLUMN "age" TYPE varchar USING "age"::varchar;
    01_schema.up.sqlite.sql     -- ferro: foreign-keys-off
                                -- ferro: data-dependent
                                CREATE TABLE IF NOT EXISTS "_ferro_new_author" (…as the step leaves it…);
                                INSERT INTO "_ferro_new_author" (…) SELECT "age", … FROM "author";
                                -- every copied "age" must now be stored as text,
                                -- or the step fails naming the column:
                                CREATE TEMP TABLE "_ferro_rebuild_guard" (…CHECK (0));
                                INSERT INTO "_ferro_rebuild_guard" … WHERE typeof("age") NOT IN ('text', 'null');
                                DROP TABLE "_ferro_rebuild_guard";
                                DROP TABLE "author";
                                ALTER TABLE "_ferro_new_author" RENAME TO "author";
                                CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_name" ON "author" ("name");
```

Every case builds a real project under ``tmp_path``, applies ``0001`` with
``ferro migrate up``, puts rows in, generates the change and checks: the
rebuild's ``CREATE TABLE`` is the create pass's for the shape the step leaves
(apart from the name) and its indexes are the create pass's (AGENTS.md
§ I-1); ``up`` applies and copies the rows; the live schema has no drift
against the new snapshot; ``down`` reverts with no drift against the parent.
The rebuild is SQLite's; the Postgres half of each case pins its rendering
byte for byte to ticket #524's.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import ferro
from ferro import _core
from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    isolated_imports,
    pkg,
    project,
    run,
    statements,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix
sqlite_only = pytest.mark.sqlite_only

REBUILD = "-- ferro: foreign-keys-off\n"
DATA_DEPENDENT = "-- ferro: data-dependent\n"


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: an edit that keeps ``models.py``'s size inside the
    same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


# -- helpers --------------------------------------------------------------------------


def step_file(project: Path, number: int, direction: str, backend: str) -> Path:
    return migration_dir(project, number) / f"01_schema.{direction}.{backend}.sql"


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models and is applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0


def generate(project: Path, pkg: str, body: str, name: str) -> int:
    """Generate the next migration for ``body``; returns its number."""
    write_models(project, pkg, body)
    new(name)
    return len(list((project / "migrations").glob("[0-9][0-9][0-9][0-9]_*")))


def fresh_create(project: Path, number: int, table: str) -> tuple[str, list[str]]:
    """What the create pass writes on SQLite for ``table`` as migration
    ``number``'s snapshot declares it: its ``CREATE TABLE`` and its indexes."""
    generated = json.loads(
        _core._generate_migration(
            None, json.dumps(snapshot(project, number)), ["sqlite"]
        )
    )
    path = project / "fresh.sql"
    path.write_text(generated["steps"][0]["renderings"]["sqlite"]["up"])
    created = statements(path)
    create = next(
        s for s in created if s.startswith(f'CREATE TABLE IF NOT EXISTS "{table}" ')
    )
    indexes = [s for s in created if "INDEX" in s and f' ON "{table}" ' in s]
    return create, indexes


def checked_copy(table: str, column: str, target: str, storage: str) -> list[str]:
    """The check a rebuild runs after copying a retyped ``column``: every
    value whose storage class is not ``storage`` fails the step, naming the
    column and its new type."""
    return [
        'CREATE TEMP TABLE "_ferro_rebuild_guard" ("value", CONSTRAINT '
        f'"ferro: {table}.{column} has a value that cannot become {target}" CHECK (0))',
        f'INSERT INTO "_ferro_rebuild_guard" ("value") SELECT "{column}" FROM '
        f"\"_ferro_new_{table}\" WHERE typeof(\"{column}\") NOT IN ('{storage}', 'null')",
        'DROP TABLE "_ferro_rebuild_guard"',
    ]


def rebuild_of(
    project: Path, number: int, table: str, copy: str, checks: list[str] | None = None
) -> list[str]:
    """The rebuild of ``table`` into migration ``number``'s shape, copying
    ``copy`` (the ``INSERT``'s column list and ``SELECT`` list, as text)
    as it stands, then running ``checks`` (from ``checked_copy``)."""
    create, indexes = fresh_create(project, number, table)
    return [
        create.replace(
            f'CREATE TABLE IF NOT EXISTS "{table}" ',
            f'CREATE TABLE IF NOT EXISTS "_ferro_new_{table}" ',
            1,
        ),
        f'INSERT INTO "_ferro_new_{table}" {copy} FROM "{table}"',
        *(checks or []),
        f'DROP TABLE "{table}"',
        f'ALTER TABLE "_ferro_new_{table}" RENAME TO "{table}"',
        *indexes,
    ]


def clean(db, project: Path, number: int, other: int) -> bool:
    """No drift between the live schema and migration ``number``'s snapshot."""
    return plan_against(db, snapshot(project, number), snapshot(project, other)) == []


def round_trip(project: Path, db, number: int) -> None:
    """``up`` applies ``number`` with no drift; ``down`` reverts it with no
    drift against its parent; ``up`` applies it again."""
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, number, number - 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db)[-1] == (number - 1, 1)
    assert clean(db, project, number - 1, number)
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db)[-1][0] == number


def fresh_foreign_keys(db) -> int:
    """``PRAGMA foreign_keys`` on a fresh ferro connection."""

    async def read() -> int:
        await ferro.connect(db.url, name="fresh")
        try:
            row = await ferro.raw.fetch_one("PRAGMA foreign_keys", using="fresh")
        finally:
            await _core._disconnect("fresh")
        assert row is not None
        return list(row.values())[0]

    return asyncio.run(read())


AGE_INT = AUTHOR + "    age: int | None = None\n"
AGE_TEXT = AUTHOR + "    age: str | None = None\n"
NICKNAME = AUTHOR + "    nickname: str\n"
OPTIONAL_NICKNAME = AUTHOR + "    nickname: str | None = None\n"
COLUMNS = '("age", "id", "name", "status")'


# -- A6: a type change ----------------------------------------------------------------


@backend_matrix
def test_a6_a_type_change_rebuilds_copying_with_a_cast_and_round_trips(
    project, pkg, db
):
    start(project, pkg, db, AGE_INT)
    db.execute("INSERT INTO author (name, status, age) VALUES ('ada', 'draft', 36)")

    number = generate(project, pkg, AGE_TEXT, "author_age_text")

    up = step_file(project, number, "up", db.backend)
    down = step_file(project, number, "down", db.backend)
    if db.backend == "postgres":
        # Byte-unchanged from #524.
        assert up.read_text() == (
            DATA_DEPENDENT + "\n"
            'ALTER TABLE "author" ALTER COLUMN "age" TYPE varchar USING "age"::varchar;\n'
        )
        assert down.read_text() == (
            DATA_DEPENDENT + "\n"
            'ALTER TABLE "author" ALTER COLUMN "age" TYPE integer USING "age"::integer;\n'
        )
        round_trip(project, db, number)
        return
    assert up.read_text().startswith(REBUILD + DATA_DEPENDENT + "\n")
    assert statements(up) == rebuild_of(
        project,
        number,
        "author",
        f'{COLUMNS} SELECT "age", "id", "name", "status"',
        checked_copy("author", "age", "varchar", "text"),
    )
    assert down.read_text().startswith(REBUILD + DATA_DEPENDENT + "\n")
    assert statements(down) == rebuild_of(
        project,
        number - 1,
        "author",
        f'{COLUMNS} SELECT "age", "id", "name", "status"',
        checked_copy("author", "age", "integer", "integer"),
    )
    assert "CAST(" not in up.read_text() + down.read_text()

    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows("SELECT name, age, typeof(age) FROM author") == [
        ("ada", "36", "text")
    ]
    assert clean(db, project, number, number - 1)
    assert keys(db) == [(1, 1), (2, 1)]
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert db.rows("SELECT name, age, typeof(age) FROM author") == [
        ("ada", 36, "integer")
    ]
    assert clean(db, project, number - 1, number)
    assert "_ferro_new_author" not in db.tables()


# -- A7b and A4: nullability ----------------------------------------------------------


@backend_matrix
def test_a7b_relaxing_not_null_rebuilds_and_its_down_fails_on_a_null(
    project, pkg, db, capsys
):
    start(project, pkg, db, NICKNAME)
    db.execute(
        "INSERT INTO author (name, status, nickname) VALUES ('ada', 'draft', 'a')"
    )

    number = generate(project, pkg, OPTIONAL_NICKNAME, "relax")

    up = step_file(project, number, "up", db.backend)
    down = step_file(project, number, "down", db.backend)
    if db.backend == "postgres":
        assert up.read_text() == (
            'ALTER TABLE "author" ALTER COLUMN "nickname" DROP NOT NULL;\n'
        )
        assert down.read_text() == (
            DATA_DEPENDENT + "\n"
            'ALTER TABLE "author" ALTER COLUMN "nickname" SET NOT NULL;\n'
        )
        return
    copy = (
        '("id", "name", "nickname", "status") SELECT "id", "name", "nickname", "status"'
    )
    assert up.read_text().startswith(REBUILD + "\n")
    assert statements(up) == rebuild_of(project, number, "author", copy)
    assert down.read_text().startswith(REBUILD + DATA_DEPENDENT + "\n")
    assert statements(down) == rebuild_of(project, number - 1, "author", copy)
    round_trip(project, db, number)
    assert db.rows("SELECT name, nickname FROM author") == [("ada", "a")]

    # A NULL the relaxed column now holds fails the down's copy, and the
    # down rolls back.
    db.execute("INSERT INTO author (name, status) VALUES ('bo', 'draft')")
    capsys.readouterr()
    assert run("migrate", "down", "--yes", "--url", db.url) == 1
    assert "NOT NULL constraint failed" in capsys.readouterr().err
    assert keys(db) == [(1, 1), (2, 1)]
    assert "_ferro_new_author" not in db.tables()
    db.execute("DELETE FROM author WHERE name = 'bo'")
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, number - 1, number)


@sqlite_only
def test_a4_a_dropped_not_null_column_comes_back_by_a_rebuild(project, pkg, db, capsys):
    start(project, pkg, db, NICKNAME)

    number = generate(project, pkg, AUTHOR, "drop_nickname")

    up = step_file(project, number, "up", db.backend)
    down = step_file(project, number, "down", db.backend)
    assert up.read_text() == (
        '-- ferro: destructive\n\nALTER TABLE "author" DROP COLUMN "nickname";\n'
    )
    assert statements(down) == rebuild_of(
        project,
        number - 1,
        "author",
        '("id", "name", "status") SELECT "id", "name", "status"',
    )
    assert down.read_text().startswith(REBUILD + DATA_DEPENDENT + "\n")
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, number, number - 1)

    # Populated: the copy gives the column no value, so the down fails and
    # stands (ADR-0033: a down restores schema, never data).
    db.execute("INSERT INTO author (name, status) VALUES ('ada', 'draft')")
    capsys.readouterr()
    assert run("migrate", "down", "--yes", "--url", db.url) == 1
    assert "NOT NULL constraint failed" in capsys.readouterr().err
    assert keys(db) == [(1, 1), (2, 1)]

    # Empty: it reaches the parent snapshot.
    db.execute("DELETE FROM author")
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, number - 1, number)


# -- A10: checks ------------------------------------------------------------------------

CHECKS = "from typing import ClassVar\n\nfrom ferro import Check\n" + AUTHOR


def checked(predicate: str) -> str:
    return (
        CHECKS
        + "    age: int | None = None\n"
        + "    __ferro_checks__: ClassVar[tuple[Check, ...]] = (\n"
        + f'        Check("age_positive", lambda author: {predicate}),\n'
        + "    )\n"
    )


@sqlite_only
def test_a10_a_table_check_added_changed_and_dropped_rebuilds_with_it_inline(
    project, pkg, db
):
    start(project, pkg, db, CHECKS + "    age: int | None = None\n")
    db.execute("INSERT INTO author (name, status, age) VALUES ('ada', 'draft', 36)")
    copy = f'{COLUMNS} SELECT "age", "id", "name", "status"'
    shapes = [
        ("add_check", checked("author.age > 0"), True),
        ("change_check", checked("author.age > 1"), True),
        ("drop_check", CHECKS + "    age: int | None = None\n", False),
    ]
    for name, body, has_check in shapes:
        number = generate(project, pkg, body, name)

        up = step_file(project, number, "up", "sqlite")
        down = step_file(project, number, "down", "sqlite")
        # Adding or changing a check can fail on the rows; dropping one cannot.
        assert up.read_text().startswith(
            REBUILD + (DATA_DEPENDENT if has_check else "") + "\n"
        )
        assert statements(up) == rebuild_of(project, number, "author", copy)
        assert ('CONSTRAINT "ck_author_age_positive" CHECK' in statements(up)[0]) is (
            has_check
        )
        assert statements(down) == rebuild_of(project, number - 1, "author", copy)
        round_trip(project, db, number)
        assert db.rows("SELECT age FROM author") == [(36,)]

    # A row the new check refuses fails the copy, and the step rolls back.
    db.execute("INSERT INTO author (name, status, age) VALUES ('bo', 'draft', -1)")
    number = generate(project, pkg, checked("author.age > 0"), "check_again")
    assert run("migrate", "up", "--url", db.url) == 1
    record = db.records()[-1]
    assert record[0] == number and record[9] is None
    assert "CHECK constraint failed: ck_author_age_positive" in record[11]
    assert "_ferro_new_author" not in db.tables()
    assert (
        "ck_author_age_positive"
        not in db.rows("SELECT sql FROM sqlite_master WHERE name = 'author'")[0][0]
    )


# -- C2, C3: foreign keys -----------------------------------------------------------------

TEAM = """
class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""

CLUB = """
class Club(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""


def on(target: str) -> str:
    return (
        TEAM
        + CLUB
        + AUTHOR
        + f'    team: Annotated[{target} | None, ForeignKey(related_name="members", '
        'on_delete="SET NULL")] = None\n'
    )


def members(target: str) -> str:
    """``target`` carries the back-reference ``on(target)`` declares."""
    body = on(target)
    head = f"class {target}(Model):\n"
    return body.replace(
        head,
        head + '    members: Relation[list["Author"]] = BackRef()\n',
        1,
    )


@sqlite_only
def test_c2_dropping_a_foreign_key_column_rebuilds_and_its_down_adds_it_back(
    project, pkg, db
):
    start(project, pkg, db, members("Team"))
    db.execute("INSERT INTO team (id, label) VALUES (1, 'core')")
    db.execute("INSERT INTO author (name, status, team_id) VALUES ('ada', 'draft', 1)")

    number = generate(project, pkg, TEAM + CLUB + AUTHOR, "drop_team")

    up = step_file(project, number, "up", "sqlite")
    down = step_file(project, number, "down", "sqlite")
    assert up.read_text().startswith(REBUILD + "-- ferro: destructive\n\n")
    assert statements(up) == rebuild_of(
        project,
        number,
        "author",
        '("id", "name", "status") SELECT "id", "name", "status"',
    )
    # Put back natively, the inline REFERENCES ADD COLUMN writes.
    assert statements(down) == [
        'ALTER TABLE "author" ADD COLUMN "team_id" integer '
        'REFERENCES "team"("id") ON DELETE SET NULL'
    ]
    round_trip(project, db, number)
    assert db.rows("SELECT name FROM author") == [("ada",)]


@sqlite_only
def test_c3_retargeting_a_foreign_key_rebuilds_with_the_new_references(
    project, pkg, db
):
    start(project, pkg, db, members("Team"))
    db.execute("INSERT INTO team (id, label) VALUES (1, 'core')")
    db.execute("INSERT INTO club (id, label) VALUES (1, 'chess')")
    db.execute("INSERT INTO author (name, status, team_id) VALUES ('ada', 'draft', 1)")

    number = generate(project, pkg, members("Club"), "author_club")

    up = step_file(project, number, "up", "sqlite")
    down = step_file(project, number, "down", "sqlite")
    copy = (
        '("id", "name", "status", "team_id") SELECT "id", "name", "status", "team_id"'
    )
    assert up.read_text().startswith(REBUILD + DATA_DEPENDENT + "\n")
    assert statements(up) == rebuild_of(project, number, "author", copy)
    assert 'REFERENCES "club"' in statements(up)[0]
    assert statements(down) == rebuild_of(project, number - 1, "author", copy)
    assert 'REFERENCES "team"' in statements(down)[0]
    round_trip(project, db, number)
    assert db.rows("SELECT name, team_id FROM author") == [("ada", 1)]


@sqlite_only
def test_a_copy_that_orphans_a_row_fails_with_the_rows_and_rolls_everything_back(
    project, pkg, db, capsys
):
    start(project, pkg, db, members("Team"))
    db.execute("INSERT INTO team (id, label) VALUES (1, 'core')")
    db.execute("INSERT INTO author (name, status, team_id) VALUES ('ada', 'draft', 1)")
    # No club 1: the retargeted reference has no row to point at.
    number = generate(project, pkg, members("Club"), "author_club")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert "PRAGMA foreign_key_check found rows that violate a foreign key" in err
    assert 'table "author" rowid 1 references "club"' in err
    record = db.records()[-1]
    assert record[0] == number and record[9] is None and record[10] is not None
    assert "_ferro_new_author" not in db.tables()
    sql = db.rows("SELECT sql FROM sqlite_master WHERE name = 'author'")[0][0]
    assert 'REFERENCES "team"' in sql, "the rebuild rolled back"
    assert fresh_foreign_keys(db) == 1


# -- one copy per table per phase step ----------------------------------------------------


@sqlite_only
def test_two_tables_rebuilt_in_one_phase_share_one_step_and_one_transaction(
    project, pkg, db
):
    def ages(logical: str) -> str:
        age = f"    age: {logical} | None = None\n"
        status = "    status: Status = Status.DRAFT\n"
        return LIBRARY.replace(status, status + age) + age

    start(project, pkg, db, ages("int"))

    number = generate(project, pkg, ages("str"), "ages_text")

    assert sorted(p.name for p in migration_dir(project, number).iterdir()) == [
        "01_schema.down.sqlite.sql",
        "01_schema.up.sqlite.sql",
        "ir.json",
    ]
    up = statements(step_file(project, number, "up", "sqlite"))
    assert [s for s in up if s.startswith("CREATE TABLE")] == [
        rebuild_of(project, number, "author", "")[0],
        rebuild_of(project, number, "post", "")[0],
    ]
    round_trip(project, db, number)
    assert keys(db)[-1] == (number, 1), "one step, one record"


@sqlite_only
def test_a_type_change_and_a_new_index_on_one_table_copy_it_once(project, pkg, db):
    email = "    email: str | None = None\n"
    indexed = "    email: Annotated[str | None, FerroField(index=True)] = None\n"
    start(project, pkg, db, AGE_INT + email)
    db.execute(
        "INSERT INTO author (name, status, age, email) VALUES ('ada', 'draft', 3, 'a@x')"
    )

    number = generate(project, pkg, AGE_TEXT + indexed, "age_and_email")

    # The rebuild recreates the table as it stands after its step; the new
    # index is its own step after it, built once over the copied rows (#527).
    built = 'CREATE INDEX IF NOT EXISTS "idx_author_email" ON "author" ("email")'
    up = statements(step_file(project, number, "up", "sqlite"))
    assert up == [
        sql
        for sql in rebuild_of(
            project,
            number,
            "author",
            '("age", "email", "id", "name", "status") '
            'SELECT "age", "email", "id", "name", "status"',
            checked_copy("author", "age", "varchar", "text"),
        )
        if sql != built
    ]
    assert sum(s.startswith("CREATE TABLE") for s in up) == 1
    assert not any('"idx_author_email"' in s for s in up)
    # The index the table already had is recreated once, after the rename.
    kept = [s for s in up if '"uq_author_name"' in s]
    assert len(kept) == 1
    assert up.index(kept[0]) > up.index(
        'ALTER TABLE "_ferro_new_author" RENAME TO "author"'
    )
    index_step = migration_dir(project, number) / "02_idx_author_email.up.sqlite.sql"
    assert statements(index_step) == [built]
    round_trip(project, db, number)
    assert db.rows("SELECT age, email FROM author") == [("3", "a@x")]


@sqlite_only
def test_a_cascading_childs_rows_survive_the_parents_rebuild(project, pkg, db):
    age = "    age: int | None = None\n"
    with_age = LIBRARY.replace(
        "    status: Status = Status.DRAFT\n",
        "    status: Status = Status.DRAFT\n" + age,
    )
    start(project, pkg, db, with_age)
    db.execute(
        "INSERT INTO author (id, name, status, age) VALUES (1, 'ada', 'draft', 1)"
    )
    db.execute(
        "INSERT INTO post (id, title, kind, author_id) VALUES (1, 't', 'note', 1)"
    )

    number = generate(
        project, pkg, with_age.replace(age, "    age: str | None = None\n"), "age"
    )

    assert rebuild_of(project, number, "author", "")[0] in statements(
        step_file(project, number, "up", "sqlite")
    )
    round_trip(project, db, number)
    assert db.rows("SELECT id, author_id FROM post") == [(1, 1)]
    assert db.rows("PRAGMA foreign_key_check") == []


# -- the runner's check before a rebuild ----------------------------------------------------


@sqlite_only
@pytest.mark.parametrize(
    ("live", "named"),
    [
        (
            "CREATE TRIGGER author_audit AFTER INSERT ON author BEGIN SELECT 1; END",
            'trigger "author_audit"',
        ),
        ('CREATE INDEX my_idx ON author ("age")', 'index "my_idx"'),
        ("ALTER TABLE author ADD COLUMN extra text", 'column "extra"'),
    ],
)
def test_a_rebuild_of_a_table_holding_an_undeclared_object_is_refused_naming_it(
    project, pkg, db, capsys, live, named
):
    start(project, pkg, db, AGE_INT)
    db.execute("INSERT INTO author (name, status, age) VALUES ('ada', 'draft', 1)")
    db.execute(live)
    generate(project, pkg, AGE_TEXT, "author_age_text")
    before = db.rows("SELECT sql FROM sqlite_master ORDER BY name")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert 'a SQLite rebuild of table "author" copies only the columns' in err
    assert f"\n  - {named}: " in err
    assert err.rstrip().endswith("Nothing was applied.")
    assert keys(db) == [(1, 1)], "refused before the step recorded anything"
    assert db.rows("SELECT sql FROM sqlite_master ORDER BY name") == before


@sqlite_only
def test_every_undeclared_object_is_named_at_once(project, pkg, db, capsys):
    start(project, pkg, db, AGE_INT)
    db.execute("CREATE TRIGGER t1 AFTER INSERT ON author BEGIN SELECT 1; END")
    db.execute('CREATE INDEX my_idx ON author ("age")')
    db.execute("ALTER TABLE author ADD COLUMN extra text")
    generate(project, pkg, AGE_TEXT, "author_age_text")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    for named in ('column "extra"', 'index "my_idx"', 'trigger "t1"'):
        assert f"\n  - {named}: " in err
    assert keys(db) == [(1, 1)]


@sqlite_only
def test_a_down_that_rebuilds_checks_the_table_as_its_migration_left_it(
    project, pkg, db, capsys
):
    start(project, pkg, db, AGE_INT)
    generate(project, pkg, AGE_TEXT, "author_age_text")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("CREATE TRIGGER t1 AFTER INSERT ON author BEGIN SELECT 1; END")
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 1

    assert '\n  - trigger "t1": ' in capsys.readouterr().err
    assert keys(db) == [(1, 1), (2, 1)], "the step's record stands"
    db.execute("DROP TRIGGER t1")
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert clean(db, project, 1, 2)


# -- a retyped column's values: copied as they stand, checked, never CAST ------------

CODE_TEXT = AUTHOR + "    code: str | None = None\n"
CODE_INT = AUTHOR + "    code: int | None = None\n"


@sqlite_only
def test_text_that_reads_as_an_integer_becomes_one(project, pkg, db):
    start(project, pkg, db, CODE_TEXT)
    db.execute("INSERT INTO author (name, status, code) VALUES ('ada', 'draft', '12')")

    number = generate(project, pkg, CODE_INT, "code_int")

    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows("SELECT code, typeof(code) FROM author") == [(12, "integer")]
    assert clean(db, project, number, number - 1)


@sqlite_only
@pytest.mark.parametrize(
    ("before", "after", "value", "target"),
    [
        (CODE_TEXT, CODE_INT, "'abc'", "integer"),
        (CODE_TEXT, CODE_INT, "'12abc'", "integer"),
        (
            AUTHOR + "    code: float | None = None\n",
            CODE_INT,
            "1.9",
            "integer",
        ),
    ],
    ids=["abc", "12abc", "1.9"],
)
def test_a_value_that_does_not_become_the_new_type_fails_the_step_and_rolls_back(
    project, pkg, db, capsys, before, after, value, target
):
    start(project, pkg, db, before)
    db.execute(
        f"INSERT INTO author (name, status, code) VALUES ('ada', 'draft', {value})"
    )
    stored = db.rows("SELECT code, typeof(code) FROM author")

    number = generate(project, pkg, after, "code_retyped")
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert (
        "CHECK constraint failed: ferro: author.code has a value that cannot become "
        f"{target}"
    ) in err
    record = db.records()[-1]
    assert record[0] == number and record[9] is None and record[10] is not None
    assert db.rows("SELECT code, typeof(code) FROM author") == stored
    assert "_ferro_new_author" not in db.tables()
    assert fresh_foreign_keys(db) == 1


@sqlite_only
def test_the_down_of_a_type_change_fails_on_a_value_it_cannot_take_back(
    project, pkg, db, capsys
):
    start(project, pkg, db, AGE_INT)
    generate(project, pkg, AGE_TEXT, "author_age_text")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("INSERT INTO author (name, status, age) VALUES ('ada', 'draft', 'abc')")
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 1

    assert "author.age has a value that cannot become integer" in (
        capsys.readouterr().err
    )
    assert keys(db) == [(1, 1), (2, 1)], "the step's record stands"
    assert db.rows("SELECT age FROM author") == [("abc",)]


DATETIME_HEADER = "from datetime import datetime\n"


@sqlite_only
def test_a_datetime_column_copied_by_a_rebuild_keeps_its_bytes(project, pkg, db):
    required = DATETIME_HEADER + AUTHOR + "    seen: datetime\n"
    optional = DATETIME_HEADER + AUTHOR + "    seen: datetime | None = None\n"
    start(project, pkg, db, required)
    # A SQLite datetime column reads back as drift straight after a fresh
    # create (this predates #526): the rebuild must add none of its own.
    fresh = plan_against(db, snapshot(project, 1), snapshot(project, 1))
    db.execute(
        "INSERT INTO author (name, status, seen) "
        "VALUES ('ada', 'draft', '2026-03-01T15:00:00Z')"
    )
    stored = [("2026-03-01T15:00:00Z", "text")]

    number = generate(project, pkg, optional, "seen_optional")

    assert "CAST(" not in step_file(project, number, "up", "sqlite").read_text()
    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows("SELECT seen, typeof(seen) FROM author") == stored
    assert plan_against(db, snapshot(project, number), snapshot(project, 1)) == fresh
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert db.rows("SELECT seen, typeof(seen) FROM author") == stored
    assert plan_against(db, snapshot(project, 1), snapshot(project, number)) == fresh


@pytest.mark.parametrize(
    ("before", "after", "change"),
    [
        (
            AUTHOR + "    seen: str | None = None\n",
            DATETIME_HEADER + AUTHOR + "    seen: datetime | None = None\n",
            "from varchar to DATETIME",
        ),
        (
            # Its down would be the change above.
            DATETIME_HEADER + AUTHOR + "    seen: datetime | None = None\n",
            AUTHOR + "    seen: str | None = None\n",
            "from varchar to DATETIME",
        ),
        (
            "from datetime import date\n" + AUTHOR + "    seen: date | None = None\n",
            DATETIME_HEADER + AUTHOR + "    seen: datetime | None = None\n",
            "from DATE to DATETIME",
        ),
        (
            # SQLite's JSON column would store '00123' as 123, valid JSON.
            AUTHOR + "    seen: str | None = None\n",
            AUTHOR + "    seen: dict | None = None\n",
            "from varchar to JSON",
        ),
        (
            AUTHOR + "    seen: dict | None = None\n",
            AUTHOR + "    seen: str | None = None\n",
            "from JSON to varchar",
        ),
    ],
    ids=[
        "str-to-datetime",
        "datetime-to-str",
        "date-to-datetime",
        "str-to-json",
        "json-to-str",
    ],
)
def test_a_type_change_sqlite_cannot_check_is_refused_naming_the_column(
    project, pkg, capsys, before, after, change
):
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = ["sqlite"]\n'
    )
    write_models(project, pkg, before)
    new("create")
    write_models(project, pkg, after)
    capsys.readouterr()

    assert run("migrate", "new", "seen") == 1

    err = capsys.readouterr().err
    assert f"a SQLite rebuild cannot change author.seen {change}" in err
    assert "fill it in a data step" in err
    assert not list((project / "migrations").glob("0002_*"))
