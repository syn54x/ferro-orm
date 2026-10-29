# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new`` for every change to a generated column on an existing table.

```python
class Transaction(Model):
    amount_minor: int
    memo: str
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.amount_minor), index=True
    )
```

| Change | Postgres | SQLite |
|---|---|---|
| add the column | `ADD COLUMN … GENERATED ALWAYS AS (…) STORED`, its index in its own step | rebuild |
| change the expression | `ALTER COLUMN … SET EXPRESSION AS (…)` | rebuild |
| make it plain | `ALTER COLUMN … DROP EXPRESSION`, the values kept | rebuild |
| make a plain column generated | `DROP COLUMN` + `ADD COLUMN`, `-- ferro: destructive` | rebuild, destructive |
| change an input's type | drop the column, `ALTER … TYPE`, add it back | rebuild |
| rename an input | the `RENAME COLUMN` alone | the rename alone |

A required generated column asks the rows already there for nothing: no
backfill step and no ``-- ferro: data-dependent``. Every case builds a
project, applies ``0001`` with a row in it, generates the change, applies it
and checks the stored values and that drift is clean, then takes it down and
checks drift against ``0001``. The casebook (``tests/_casebook.py``) builds
its generated-column cases from the bodies here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_migrate_down import migration_dir  # noqa: F401
from tests.test_migrate_drift import drift_api
from tests.test_migrate_new import (  # noqa: F401 - fixtures
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

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry", "no_bytecode"),
    pytest.mark.backend_matrix,
]


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: an edit that keeps ``models.py``'s size inside the
    same second would otherwise import the stale one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


HEAD = """
from ferro import Field, fn


class Transaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount_minor: int
    memo: str
"""

WIDE_HEAD = HEAD.replace(
    "    amount_minor: int\n",
    '    amount_minor: Annotated[int, FerroField(db_type="bigint")]\n',
)

RENAMED_HEAD = HEAD.replace(
    "    amount_minor: int\n",
    '    amount: Annotated[int, FerroField(renamed_from="amount_minor")]\n',
)


def direction(function: str = "sign", column: str = "amount_minor") -> str:
    """The indexed generated ``direction`` column, ``<function>(<column>)``."""
    return (
        "    direction: int = Field(\n"
        f"        generated=lambda transaction: fn.{function}(transaction.{column}),\n"
        "        index=True,\n"
        "    )\n"
    )


PLAIN_DIRECTION = "    direction: Annotated[int, FerroField(index=True)]\n"

INDEXED = HEAD + direction()
ABS = HEAD + direction("abs")
WIDE = WIDE_HEAD + direction()
RENAMED = RENAMED_HEAD + direction(column="amount")
PLAIN = HEAD + PLAIN_DIRECTION

NONZERO = (
    "    __ferro_checks__: ClassVar[tuple[Check, ...]] = (\n"
    '        Check("nonzero", lambda transaction: transaction.direction != 0),\n'
    "    )\n"
)
"""A table check reading the generated column: Postgres drops it with the
column, so a drop and add of the column must add it back."""


def checked(head: str) -> str:
    return (
        "from typing import ClassVar\n\nfrom ferro import Check\n"
        + head
        + direction()
        + NONZERO
    )


CHECKED = checked(HEAD)
CHECKED_WIDE = checked(WIDE_HEAD)


# -- helpers --------------------------------------------------------------------------


def start(project: Path, pkg: str, db, body: str) -> None:
    """``0001`` creates ``body``'s models and is applied, with one row."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, body)
    new("create")
    assert run("migrate", "up", "--url", db.url) == 0
    columns = '"amount_minor", "memo"'
    values = "-5, 'refund'"
    if "direction: Annotated" in body:
        columns, values = columns + ', "direction"', values + ", -1"
    db.execute(f'INSERT INTO "transaction" ({columns}) VALUES ({values})')


def generate(project: Path, pkg: str, body: str, name: str) -> Path:
    """Generate the next migration for ``body``; returns its directory."""
    write_models(project, pkg, body)
    new(name)
    return sorted((project / "migrations").glob("[0-9][0-9][0-9][0-9]_*"))[-1]


def up_files(migration: Path, backend: str) -> dict[str, str]:
    """Each up step file of ``migration`` on ``backend``, by step name
    (``01_schema``), as written."""
    return {
        path.name.split(".")[0]: path.read_text()
        for path in sorted(migration.glob(f"*.up.{backend}.sql"))
    }


def up_statements(migration: Path, backend: str) -> list[str]:
    return [
        statement
        for path in sorted(migration.glob(f"*.up.{backend}.sql"))
        for statement in statements(path)
    ]


def directions(db) -> list[tuple[int, int]]:
    return db.rows('SELECT "amount_minor", "direction" FROM "transaction"')


def applied_and_reverted(db) -> None:
    """``up`` applies the change with no drift after it; ``down`` takes it
    back with no drift against ``0001``."""
    assert run("migrate", "up", "--url", db.url) == 0
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


# -- the table, row by row ----------------------------------------------------------


def test_adding_an_indexed_generated_column_to_a_table_with_rows(project, pkg, db):
    start(project, pkg, db, HEAD)
    migration = generate(project, pkg, INDEXED, "direction")

    files = up_files(migration, db.backend)
    # A required generated column asks the row already there for nothing.
    assert not any("backfill" in step for step in files), files
    assert all("-- ferro: data-dependent" not in text for text in files.values())
    if db.backend == "postgres":
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
            'GENERATED ALWAYS AS (sign(CAST("amount_minor" AS bigint))) STORED',
            # The index step drops what a failed build left, then builds.
            'DROP INDEX CONCURRENTLY IF EXISTS "idx_transaction_direction"',
            'CREATE INDEX CONCURRENTLY "idx_transaction_direction" '
            'ON "transaction" ("direction")',
        ]
    else:
        assert files["01_schema"].startswith("-- ferro: foreign-keys-off\n")

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_changing_the_expression_recomputes_and_its_down_restores_it(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, ABS, "abs_direction")

    if db.backend == "postgres":
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" ALTER COLUMN "direction" SET EXPRESSION AS '
            '(abs(CAST("amount_minor" AS bigint)))'
        ]
    else:
        assert up_files(migration, "sqlite")["01_schema"].startswith(
            "-- ferro: foreign-keys-off\n"
        )

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, 5)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []


def test_making_a_generated_column_plain_keeps_its_values(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, PLAIN, "plain_direction")

    files = up_files(migration, db.backend)
    assert all("-- ferro: destructive" not in text for text in files.values())
    if db.backend == "postgres":
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" ALTER COLUMN "direction" DROP EXPRESSION'
        ]

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    db.execute('UPDATE "transaction" SET "direction" = 7')
    assert directions(db) == [(-5, 7)]
    assert drift_api(db).lines == []
    # The down makes it generated again, computing every row.
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []


def test_making_a_plain_column_generated_is_destructive(project, pkg, db):
    start(project, pkg, db, PLAIN)
    migration = generate(project, pkg, INDEXED, "generated_direction")

    files = up_files(migration, db.backend)
    assert files["01_schema"].count("-- ferro: destructive\n") == 1, files
    if db.backend == "postgres":
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" DROP COLUMN "direction"',
            'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
            'GENERATED ALWAYS AS (sign(CAST("amount_minor" AS bigint))) STORED',
            # The index step drops what a failed build left, then builds.
            'DROP INDEX CONCURRENTLY IF EXISTS "idx_transaction_direction"',
            'CREATE INDEX CONCURRENTLY "idx_transaction_direction" '
            'ON "transaction" ("direction")',
        ]

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_an_input_type_change_drops_alters_and_adds_back(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, WIDE, "wide_amount")

    if db.backend == "postgres":
        files = up_files(migration, "postgres")
        assert all("-- ferro: destructive" not in text for text in files.values())
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" DROP COLUMN "direction"',
            'ALTER TABLE "transaction" ALTER COLUMN "amount_minor" TYPE bigint '
            'USING "amount_minor"::bigint',
            'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
            'GENERATED ALWAYS AS (sign("amount_minor")) STORED',
            # The index step drops what a failed build left, then builds.
            'DROP INDEX CONCURRENTLY IF EXISTS "idx_transaction_direction"',
            'CREATE INDEX CONCURRENTLY "idx_transaction_direction" '
            'ON "transaction" ("direction")',
        ]

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []


def refuses_a_zero_direction(db) -> None:
    """The ``nonzero`` check stands: a row whose ``direction`` is 0 fails."""
    with pytest.raises(Exception, match="(?i)check|constraint"):
        db.execute(
            'INSERT INTO "transaction" ("amount_minor", "memo") VALUES (0, \'x\')'
        )


def test_a_table_check_over_the_column_comes_back_around_an_input_type_change(
    project, pkg, db
):
    start(project, pkg, db, CHECKED)
    migration = generate(project, pkg, CHECKED_WIDE, "wide_checked")

    if db.backend == "postgres":
        added = up_statements(migration, "postgres")
        at = {
            what: next(i for i, s in enumerate(added) if needle in s)
            for what, needle in (
                ("drop", 'DROP COLUMN "direction"'),
                ("add", 'ADD COLUMN "direction"'),
                ("check", '"ck_transaction_nonzero"'),
            )
        }
        assert at["drop"] < at["add"] < at["check"], added

    assert run("migrate", "up", "--url", db.url) == 0
    refuses_a_zero_direction(db)
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    refuses_a_zero_direction(db)
    assert drift_api(db).lines == []


def test_a_renamed_input_column_is_the_rename_alone(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, RENAMED, "amount")

    assert up_statements(migration, db.backend) == [
        'ALTER TABLE "transaction" RENAME COLUMN "amount_minor" TO "amount"'
    ]
    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows('SELECT "amount", "direction" FROM "transaction"') == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_dropping_a_generated_column_is_destructive(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, HEAD, "drop_direction")

    files = up_files(migration, db.backend)
    assert any("-- ferro: destructive\n" in text for text in files.values()), files
    applied_and_reverted(db)
    assert directions(db) == [(-5, -1)]
