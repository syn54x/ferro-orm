# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Online-safe constraints and indexes on an existing table (#527; ADR-0043,
ADR-0044).

```text
$ ferro migrate new author_email_unique
  0002_author_email_unique/
    01_schema.up.postgres.sql          ALTER TABLE "author" ADD CONSTRAINT "ck_author_email_nonempty" CHECK (…) NOT VALID;
    02_uq_author_email.up.postgres.sql -- ferro: no-transaction
                                       -- ferro: data-dependent
                                       DROP INDEX CONCURRENTLY IF EXISTS "uq_author_email";
                                       CREATE UNIQUE INDEX CONCURRENTLY "uq_author_email" ON "author" ("email");
    03_validate.up.postgres.sql        -- ferro: data-dependent
                                       ALTER TABLE "author" VALIDATE CONSTRAINT "ck_author_email_nonempty";
    02_uq_author_email.up.sqlite.sql   -- ferro: data-dependent
                                       CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email");
    03_validate.up.sqlite.sql          -- ferro: not-applicable
```

Every case builds a real project under ``tmp_path`` targeting both dialects,
applies ``0001`` with ``ferro migrate up`` against the parametrized database,
generates the change and checks the files, then that ``up`` applies, the live
schema has no drift against the new snapshot, ``down`` reverts with no drift
against the parent, and ``up`` applies again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_migrate_down import (  # noqa: F401 - fixtures
    keys,
    migration_dir,
    plan_against,
    snapshot,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    listing,
    pkg,
    project,
    run,
    statements,
    write_config,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    db,
    new,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)

backend_matrix = pytest.mark.backend_matrix
postgres_only = pytest.mark.postgres_only


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


# -- models -------------------------------------------------------------------------

HEAD = "from typing import ClassVar\n\nfrom ferro import Check\n" + AUTHOR
EMAIL = "    email: str | None = None\n"
UNIQUE_EMAIL = "    email: Annotated[str | None, FerroField(unique=True)] = None\n"
NONEMPTY = (
    "    __ferro_checks__: ClassVar[tuple[Check, ...]] = (\n"
    '        Check("email_nonempty", lambda author: author.email != None),\n'
    "    )\n"
)
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

CHECK_ADD = (
    'ALTER TABLE "author" ADD CONSTRAINT "ck_author_email_nonempty" '
    'CHECK ("email" IS NOT NULL)'
)
VALIDATE_CHECK = 'ALTER TABLE "author" VALIDATE CONSTRAINT "ck_author_email_nonempty"'
DROP_CHECK = 'ALTER TABLE "author" DROP CONSTRAINT "ck_author_email_nonempty"'
UQ_PLAIN = 'CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email")'
UQ_CONCURRENT = (
    'CREATE UNIQUE INDEX CONCURRENTLY "uq_author_email" ON "author" ("email")'
)
UQ_DROP = 'DROP INDEX CONCURRENTLY IF EXISTS "uq_author_email"'


# -- helpers ------------------------------------------------------------------------


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models for both dialects and is applied."""
    write_config(project, pkg)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0


def generate(project: Path, pkg: str, body: str, name: str) -> Path:
    """Generate ``0002`` for ``body``; returns its directory."""
    write_models(project, pkg, body)
    new(name)
    return migration_dir(project, 2)


def text(migration: Path, step: str, direction: str, dialect: str) -> str:
    return (migration / f"{step}.{direction}.{dialect}.sql").read_text()


def files(migration: Path, *steps: str) -> list[str]:
    return sorted(
        [
            f"{step}.{direction}.{dialect}.sql"
            for step in steps
            for direction in ("up", "down")
            for dialect in ("postgres", "sqlite")
        ]
        + ["ir.json"]
    )


def clean(db, project: Path, number: int, other: int) -> bool:
    """No drift between the live schema and migration ``number``'s snapshot."""
    return plan_against(db, snapshot(project, number), snapshot(project, other)) == []


def round_trip(project: Path, db, steps: int) -> None:
    """``up`` applies ``0002``'s ``steps`` steps with no drift; ``down``
    reverts them with no drift against the parent; ``up`` applies them
    again."""
    assert run("migrate", "up", "--url", db.url) == 0
    assert keys(db) == [(1, 1), *[(2, n) for n in range(1, steps + 1)]]
    assert clean(db, project, 2, 1)
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert keys(db) == [(1, 1)]
    assert clean(db, project, 1, 2)
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, 2, 1)


def index_valid(db, name: str) -> list[tuple]:
    """``pg_index.indisvalid`` of the index ``name`` in the test's schema."""
    return db.rows(
        f"SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('\"{name}\"')"
    )


# -- A10: a check on an existing table ----------------------------------------------


@backend_matrix
def test_a10_a_check_is_added_not_valid_and_validated_by_its_own_step(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD + EMAIL)

    migration = generate(project, pkg, HEAD + EMAIL + NONEMPTY, "author_email_check")

    assert listing(migration) == files(migration, "01_schema", "02_validate")
    assert text(migration, "01_schema", "up", "postgres") == f"{CHECK_ADD} NOT VALID;\n"
    assert text(migration, "01_schema", "down", "postgres") == f"{DROP_CHECK};\n"
    assert text(migration, "02_validate", "up", "postgres") == (
        f"-- ferro: data-dependent\n\n{VALIDATE_CHECK};\n"
    )
    # No un-validate: the down puts the constraint back as 01 left it.
    assert text(migration, "02_validate", "down", "postgres") == (
        f"{DROP_CHECK};\n\n{CHECK_ADD} NOT VALID;\n"
    )
    # SQLite validates by rebuilding in 01; its 02 is the one-line file.
    assert text(migration, "01_schema", "up", "sqlite").startswith(
        "-- ferro: foreign-keys-off\n"
    )
    for direction in ("up", "down"):
        assert text(migration, "02_validate", direction, "sqlite") == (
            "-- ferro: not-applicable\n"
        )

    round_trip(project, db, steps=2)
    # Both dialects count the validate step: it has a record either way.
    capsys.readouterr()
    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    out = capsys.readouterr().out
    assert "01_schema" in out and "02_validate" in out, out


@pytest.mark.sqlite_only
def test_a_sqlite_only_project_gets_no_validate_step(project, pkg, db):
    write_config(project, pkg, '["sqlite"]')
    write_models(project, pkg, HEAD + EMAIL)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0

    migration = generate(project, pkg, HEAD + EMAIL + NONEMPTY, "author_email_check")

    assert listing(migration) == [
        "01_schema.down.sqlite.sql",
        "01_schema.up.sqlite.sql",
        "ir.json",
    ]
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, 2, 1)


# -- A9: an index on an existing table ----------------------------------------------


@backend_matrix
def test_the_transcript_schema_index_step_then_validate_round_trips(project, pkg, db):
    start(project, pkg, db, HEAD + EMAIL)

    migration = generate(
        project, pkg, HEAD + UNIQUE_EMAIL + NONEMPTY, "author_email_unique"
    )

    assert listing(migration) == files(
        migration, "01_schema", "02_uq_author_email", "03_validate"
    )
    assert text(migration, "01_schema", "up", "postgres") == f"{CHECK_ADD} NOT VALID;\n"
    assert text(migration, "02_uq_author_email", "up", "postgres") == (
        "-- ferro: no-transaction\n-- ferro: data-dependent\n\n"
        f"{UQ_DROP};\n\n{UQ_CONCURRENT};\n"
    )
    assert text(migration, "02_uq_author_email", "down", "postgres") == (
        f"-- ferro: no-transaction\n\n{UQ_DROP};\n"
    )
    assert text(migration, "03_validate", "up", "postgres") == (
        f"-- ferro: data-dependent\n\n{VALIDATE_CHECK};\n"
    )
    assert text(migration, "02_uq_author_email", "up", "sqlite") == (
        f"-- ferro: data-dependent\n\n{UQ_PLAIN};\n"
    )
    assert (
        text(migration, "03_validate", "up", "sqlite") == "-- ferro: not-applicable\n"
    )

    round_trip(project, db, steps=3)
    if db.backend == "postgres":
        # Built with nothing wrapped: CONCURRENTLY refuses a transaction.
        assert db.records()[2][4] == "ddl-no-transaction"
        assert index_valid(db, "uq_author_email") == [(True,)]


@backend_matrix
def test_dropping_a_unique_is_the_reverse_index_step(project, pkg, db):
    start(project, pkg, db, HEAD + UNIQUE_EMAIL)

    migration = generate(project, pkg, HEAD + EMAIL, "author_email_plain")

    assert listing(migration) == files(migration, "01_uq_author_email")
    assert text(migration, "01_uq_author_email", "up", "postgres") == (
        f"-- ferro: no-transaction\n\n{UQ_DROP};\n"
    )
    assert text(migration, "01_uq_author_email", "down", "postgres") == (
        "-- ferro: no-transaction\n-- ferro: data-dependent\n\n"
        f"{UQ_DROP};\n\n{UQ_CONCURRENT};\n"
    )
    assert statements(migration / "01_uq_author_email.up.sqlite.sql") == [
        'DROP INDEX IF EXISTS "uq_author_email"'
    ]
    assert statements(migration / "01_uq_author_email.down.sqlite.sql") == [UQ_PLAIN]
    round_trip(project, db, steps=1)


@backend_matrix
def test_an_index_on_a_table_the_migration_creates_stays_inline(project, pkg, db):
    start(project, pkg, db, HEAD)

    migration = generate(
        project,
        pkg,
        HEAD
        + TEAM.replace(
            "    label: str\n",
            "    label: Annotated[str, FerroField(unique=True)]\n",
        ),
        "team",
    )

    assert listing(migration) == files(migration, "01_schema")
    for dialect in ("postgres", "sqlite"):
        assert (
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_team_label" ON "team" ("label")'
            in statements(migration / f"01_schema.up.{dialect}.sql")
        )
    round_trip(project, db, steps=1)


@postgres_only
def test_a_crashed_builds_invalid_leftover_is_cleaned_by_the_rerun(project, pkg, db):
    start(project, pkg, db, HEAD + EMAIL)
    generate(project, pkg, HEAD + UNIQUE_EMAIL, "author_email_unique")
    # What a build that died mid-way leaves: the index, present and invalid.
    db.execute(f"{UQ_PLAIN.replace(' IF NOT EXISTS', '')}")
    db.execute(
        "UPDATE pg_index SET indisvalid = false "
        "WHERE indexrelid = '\"uq_author_email\"'::regclass"
    )
    assert index_valid(db, "uq_author_email") == [(False,)]

    assert run("migrate", "up", "--url", db.url) == 0

    assert index_valid(db, "uq_author_email") == [(True,)]
    assert clean(db, project, 2, 1)


# -- C1, C3: foreign keys on an existing table --------------------------------------

TEAM_ID = (
    '    team: Annotated[Team | None, ForeignKey(related_name="members", '
    'on_delete="SET NULL")] = None\n'
)


@backend_matrix
def test_c1_an_optional_foreign_key_is_added_not_valid_then_validated(project, pkg, db):
    start(project, pkg, db, TEAM + HEAD)
    body = TEAM + '    members: Relation[list["Author"]] = BackRef()\n' + HEAD + TEAM_ID

    migration = generate(project, pkg, body, "author_team")

    assert listing(migration) == files(migration, "01_schema", "02_validate")
    assert statements(migration / "01_schema.up.postgres.sql") == [
        'ALTER TABLE "author" ADD COLUMN "team_id" integer',
        'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_team" FOREIGN KEY '
        '("team_id") REFERENCES "team" ("id") ON DELETE SET NULL NOT VALID',
    ]
    assert statements(migration / "02_validate.up.postgres.sql") == [
        'ALTER TABLE "author" VALIDATE CONSTRAINT "fk_author_team_id_team"'
    ]
    round_trip(project, db, steps=2)


@postgres_only
def test_c3_a_retargeted_foreign_key_drops_adds_not_valid_and_validates(
    project, pkg, db
):
    def body(target: str) -> str:
        return (
            TEAM
            + CLUB
            + HEAD
            + f'    team: Annotated[{target} | None, ForeignKey(related_name="authors", '
            'on_delete="SET NULL")] = None\n'
        ).replace(
            "    label: str\n",
            '    label: str\n    authors: Relation[list["Author"]] = BackRef()\n',
        )

    start(project, pkg, db, body("Team"))

    migration = generate(project, pkg, body("Club"), "author_club")

    assert statements(migration / "01_schema.up.postgres.sql") == [
        'ALTER TABLE "author" DROP CONSTRAINT "fk_author_team_id_team"',
        'ALTER TABLE "author" ADD CONSTRAINT "fk_author_team_id_club" FOREIGN KEY '
        '("team_id") REFERENCES "club" ("id") ON DELETE SET NULL NOT VALID',
    ]
    assert statements(migration / "02_validate.up.postgres.sql") == [
        'ALTER TABLE "author" VALIDATE CONSTRAINT "fk_author_team_id_club"'
    ]
    round_trip(project, db, steps=2)


# -- failures: counted, with the recipe ---------------------------------------------


@postgres_only
def test_a_validate_over_violating_rows_names_the_count_and_resumes_at_the_step(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD + EMAIL)
    for name in ("ada", "bob", "cy"):
        db.execute(f"INSERT INTO author (name, status) VALUES ('{name}', 'draft')")
    generate(project, pkg, HEAD + UNIQUE_EMAIL + NONEMPTY, "author_email_unique")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert "0002_author_email_unique/03_validate.up.postgres.sql failed" in err, err
    assert (
        '3 rows violate "ck_author_email_nonempty"; fix them and run ferro migrate up '
        "to resume at 0002_author_email_unique:03"
    ) in err, err
    assert keys(db) == [(1, 1), (2, 1), (2, 2), (2, 3)]

    # NOT VALID already refuses a new violating write, but lets the fix in.
    db.execute("UPDATE author SET email = name || '@x'")
    assert run("migrate", "up", "--url", db.url) == 0
    assert clean(db, project, 2, 1)


@postgres_only
def test_a_unique_build_over_duplicates_names_the_count_and_its_leftover_is_dropped(
    project, pkg, db, capsys
):
    start(project, pkg, db, HEAD + EMAIL)
    for name, email in (("a1", "a@x"), ("a2", "a@x"), ("b1", "b@x"), ("b2", "b@x")):
        db.execute(
            f"INSERT INTO author (name, status, email) VALUES ('{name}', 'draft', '{email}')"
        )
    generate(project, pkg, HEAD + UNIQUE_EMAIL, "author_email_unique")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert (
        '2 values are duplicated under "uq_author_email"; fix the rows and run ferro '
        "migrate up to resume at 0002_author_email_unique:01"
    ) in err, err
    # The failed concurrent build left its invalid index behind.
    assert index_valid(db, "uq_author_email") == [(False,)]

    db.execute("DELETE FROM author WHERE name IN ('a2', 'b2')")
    assert run("migrate", "up", "--url", db.url) == 0
    assert index_valid(db, "uq_author_email") == [(True,)]
    assert clean(db, project, 2, 1)
