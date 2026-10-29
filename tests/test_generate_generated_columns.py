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

A required generated column asks the rows already there for nothing (no
backfill step), but computing it can fail on any of them (an overflow), so
its step is ``-- ferro: data-dependent``. Every case builds a
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

FEE = HEAD + (
    "    fee_minor: int | None = None\n"
    "    direction: int | None = Field(\n"
    "        generated=lambda transaction: transaction.fee_minor + 1,\n"
    "    )\n"
)
"""A generated column added with the input it reads, which sorts after it:
the input's add goes first, and the down drops the column first."""

NO_AMOUNT = HEAD.replace("    amount_minor: int\n", "")
"""``INDEXED`` with ``direction`` and its input ``amount_minor`` dropped:
the input sorts first, and the column goes first."""

OWN_WIDE = HEAD + direction().replace(
    "        index=True,\n", '        index=True,\n        db_type="bigint",\n'
)
"""``INDEXED`` with ``direction``'s own type widened, which Postgres refuses
to alter on a generated column: it is dropped and added back."""


def policed(head: str, policy: str) -> str:
    """``head`` and ``direction`` under the one row policy ``policy``."""
    return "from ferro import RowPolicy, RowSecurity\n" + (head + direction()).replace(
        "class Transaction(Model):\n",
        f"class Transaction(Model):\n    __ferro_rls__ = RowSecurity({policy})\n",
    )


DIRECTION_POLICY = 'RowPolicy(column="direction", setting="app.direction")'
RAW_POLICY = 'RowPolicy(name="signed", using=\'"direction" >= -1\')'
POLICED = policed(HEAD, DIRECTION_POLICY)
POLICED_WIDE = policed(WIDE_HEAD, DIRECTION_POLICY)
RAW_POLICED = policed(HEAD, RAW_POLICY)
RAW_POLICED_WIDE = policed(WIDE_HEAD, RAW_POLICY)

CHECKED_CODE = """
from ferro import Check, fn


class Ticket(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    code: int
    __ferro_checks__ = (Check("coded", lambda ticket: ticket.code > 0),)
"""
CHECKED_CODE_TEXT = """
from ferro import Check, fn


class Ticket(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    code: str
    __ferro_checks__ = (Check("coded", lambda ticket: fn.length(ticket.code) > 0),)
"""
"""``CHECKED_CODE`` with the column's type and the check's body changed
together: the old body does not type-check against the new column, so the
check is dropped before the alter and added back after."""


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
    # Computing it for every row can fail on one (an overflow).
    assert "-- ferro: data-dependent\n" in files["01_schema"], files
    if db.backend == "postgres":
        assert up_statements(migration, "postgres") == [
            'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
            'GENERATED ALWAYS AS (CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)) STORED',
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
            'GENERATED ALWAYS AS (CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)) STORED',
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
            'GENERATED ALWAYS AS (CAST(sign("amount_minor") AS bigint)) STORED',
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


def positions(statements: list[str], *needles: str) -> list[int]:
    """The index of the first statement holding each of ``needles``."""
    return [
        next(i for i, statement in enumerate(statements) if needle in statement)
        for needle in needles
    ]


def test_a_generated_column_added_with_its_input_is_added_after_it(project, pkg, db):
    start(project, pkg, db, HEAD)
    migration = generate(project, pkg, FEE, "fee")

    if db.backend == "postgres":
        added = up_statements(migration, "postgres")
        fee, direction = positions(
            added, 'ADD COLUMN "fee_minor"', 'ADD COLUMN "direction"'
        )
        assert fee < direction, added
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute('UPDATE "transaction" SET "fee_minor" = 4')
    assert db.rows('SELECT "fee_minor", "direction" FROM "transaction"') == [(4, 5)]
    assert drift_api(db).lines == []
    # The down drops the column before the input it reads.
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_a_generated_column_dropped_with_its_input_is_dropped_before_it(
    project, pkg, db
):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, NO_AMOUNT, "no_amount")

    if db.backend == "postgres":
        dropped = up_statements(migration, "postgres")
        direction, amount = positions(
            dropped, 'DROP COLUMN "direction"', 'DROP COLUMN "amount_minor"'
        )
        assert direction < amount, dropped
    assert run("migrate", "up", "--url", db.url) == 0
    assert drift_api(db).lines == []
    # The down adds back a required input no row has a value for: empty the
    # table, and it adds the input before the column reading it.
    db.execute('DELETE FROM "transaction"')
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_a_generated_columns_own_type_change_drops_and_adds_it_back(project, pkg, db):
    start(project, pkg, db, INDEXED)
    migration = generate(project, pkg, OWN_WIDE, "wide_direction")

    files = up_files(migration, db.backend)
    assert all("-- ferro: destructive" not in text for text in files.values())
    if db.backend == "postgres":
        changed = up_statements(migration, "postgres")
        assert not any("USING" in statement for statement in changed), changed
        drop, add = positions(
            changed, 'DROP COLUMN "direction"', 'ADD COLUMN "direction"'
        )
        assert drop < add and '"direction" bigint' in changed[add], changed

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []


def test_a_policy_over_a_re_added_generated_column_is_dropped_and_added_back(
    project, pkg, db
):
    start(project, pkg, db, POLICED)
    migration = generate(project, pkg, POLICED_WIDE, "wide_policed")

    if db.backend == "postgres":
        changed = up_statements(migration, "postgres")
        at = positions(
            changed,
            'DROP POLICY "rls_transaction_direction"',
            'DROP COLUMN "direction"',
            'ADD COLUMN "direction"',
            'CREATE POLICY "rls_transaction_direction"',
        )
        assert at == sorted(at), changed

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_a_raw_policy_over_a_re_added_generated_column_is_dropped_and_added_back(
    project, pkg, db
):
    # Both of its bodies are the declaration's, so a migration carries it
    # (the reconciliation pass refuses: a live raw body is never rewritten).
    start(project, pkg, db, RAW_POLICED)
    migration = generate(project, pkg, RAW_POLICED_WIDE, "wide_raw")

    if db.backend == "postgres":
        changed = up_statements(migration, "postgres")
        at = positions(
            changed,
            'DROP POLICY "rls_transaction_signed"',
            'DROP COLUMN "direction"',
            'ADD COLUMN "direction"',
            'CREATE POLICY "rls_transaction_signed"',
        )
        assert at == sorted(at), changed

    assert run("migrate", "up", "--url", db.url) == 0
    assert directions(db) == [(-5, -1)]
    assert drift_api(db).lines == []
    assert run("migrate", "down", "--yes", "--url", db.url) == 0
    assert drift_api(db).lines == []


def test_drift_says_nothing_of_a_policy_carried_around_a_replace(project, pkg, db):
    if db.backend != "postgres":
        pytest.skip("row policies are Postgres's")
    start(project, pkg, db, POLICED)
    # The input retyped by hand, the column and its policy put back alike.
    [(using,)] = db.rows(
        "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy "
        "WHERE polname = 'rls_transaction_direction'"
    )
    [(expression,)] = db.rows(
        "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef d JOIN pg_attribute a "
        "ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE a.attname = 'direction' AND d.adrelid = '\"transaction\"'::regclass"
    )
    db.execute('DROP POLICY "rls_transaction_direction" ON "transaction"')
    db.execute('ALTER TABLE "transaction" DROP COLUMN "direction"')
    db.execute('ALTER TABLE "transaction" ALTER COLUMN "amount_minor" TYPE bigint')
    db.execute(
        f'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
        f"GENERATED ALWAYS AS ({expression}) STORED"
    )
    db.execute(
        f'CREATE POLICY "rls_transaction_direction" ON "transaction" '
        f"USING ({using}) WITH CHECK ({using})"
    )

    report = drift_api(db)

    # The policy is live and declared: its carried drop and add say nothing.
    assert report.lines == [
        "transaction.amount_minor has type bigint, snapshot says int"
    ], report.lines
    assert {op["kind"] for op in report.operations if op.get("carried")} == {
        "DropRowPolicy",
        "AddRowPolicy",
    }
