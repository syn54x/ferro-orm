# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The reconciliation pass for every change to a generated column on an existing table.

```python
class Transaction(Model):
    amount_minor: int
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.amount_minor), index=True
    )
```

On Postgres, `migrate_updates` adds the column, changes its expression,
makes it plain (its values kept) and drops and adds it back around an
input's type change; making a plain column generated replaces its values,
so only `migrate_destructive` does it. SQLite changes a generated column
only by rebuilding the table, which the pass never does: it warns
`SqliteInPlace` and runs no DDL, as for every other rebuild.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys

import pytest

import ferro
from ferro import PassReport
from tests._pass_harness import schema_sql, warned_auto_migrate
from tests.test_generate_generated_columns import (
    ABS,
    CHECKED,
    CHECKED_WIDE,
    FEE,
    HEAD,
    INDEXED,
    OWN_WIDE,
    PLAIN,
    POLICED,
    POLICED_WIDE,
    RAW_POLICED,
    RAW_POLICED_WIDE,
    WIDE,
)
from tests.test_generate_generated_columns import (
    CHECKED_CODE,
    CHECKED_CODE_TEXT,
)
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import db  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

ADD_DIRECTION = (
    'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
    'GENERATED ALWAYS AS (CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)) STORED'
)
INDEX_DIRECTION = (
    'CREATE INDEX IF NOT EXISTS "idx_transaction_direction" '
    'ON "transaction" ("direction")'
)


def declare(project, pkg, body: str) -> None:
    """``body`` as the registered models."""
    write_models(project, pkg, body)
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    sys.modules.pop(f"{pkg}.models", None)
    importlib.import_module(f"{pkg}.models")


def built(project, pkg, db, body: str) -> None:
    """``body``'s tables built by the pass, with one row."""
    declare(project, pkg, body)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    columns, values = '"amount_minor", "memo"', "-5, 'refund'"
    if body == PLAIN:
        columns, values = columns + ', "direction"', values + ", -1"
    db.execute(f'INSERT INTO "transaction" ({columns}) VALUES ({values})')


def reconnect(db, *, destructive: bool = False) -> PassReport:
    """What ``connect(migrate_updates=True, migrate_destructive=…)`` runs."""
    report = asyncio.run(
        warned_auto_migrate(db.url, updates=True, destructive=destructive)
    )
    ferro.reset_engine()
    return report


def directions(db) -> list[tuple[int, int]]:
    return db.rows('SELECT "amount_minor", "direction" FROM "transaction"')


def sqlite_warns_and_runs_nothing(report: PassReport) -> None:
    assert schema_sql(report) == []
    assert [warning.kind for warning in report.warnings] == ["SqliteInPlace"]
    assert report.warnings[0].subject.column == "direction"
    assert "ferro migrate new" in str(report.warnings[0])


def test_migrate_updates_adds_a_generated_column(project, pkg, db):
    built(project, pkg, db, HEAD)
    declare(project, pkg, INDEXED)

    report = reconnect(db)

    if db.backend == "sqlite":
        sqlite_warns_and_runs_nothing(report)
        return
    assert schema_sql(report) == [ADD_DIRECTION, INDEX_DIRECTION]
    assert list(report.warnings) == []
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db)) == []


def test_migrate_updates_changes_the_expression(project, pkg, db):
    built(project, pkg, db, INDEXED)
    declare(project, pkg, ABS)

    report = reconnect(db)

    if db.backend == "sqlite":
        sqlite_warns_and_runs_nothing(report)
        return
    assert schema_sql(report) == [
        'ALTER TABLE "transaction" ALTER COLUMN "direction" SET EXPRESSION AS '
        '(abs(CAST("amount_minor" AS bigint)))'
    ]
    assert directions(db) == [(-5, 5)]
    assert schema_sql(reconnect(db)) == []


def test_migrate_updates_makes_a_generated_column_plain_keeping_its_values(
    project, pkg, db
):
    built(project, pkg, db, INDEXED)
    declare(project, pkg, PLAIN)

    report = reconnect(db)

    if db.backend == "sqlite":
        sqlite_warns_and_runs_nothing(report)
        return
    assert schema_sql(report) == [
        'ALTER TABLE "transaction" ALTER COLUMN "direction" DROP EXPRESSION'
    ]
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db)) == []


def test_a_plain_column_becomes_generated_only_under_migrate_destructive(
    project, pkg, db
):
    built(project, pkg, db, PLAIN)
    declare(project, pkg, INDEXED)

    # Replacing the column drops its values, so `migrate_updates` leaves it
    # and says so, naming the fix: every insert now leaves it out, which its
    # NOT NULL refuses.
    kept = reconnect(db)
    assert schema_sql(kept) == []
    [warning] = kept.warnings
    assert warning.kind == "PlainColumnKept"
    assert (warning.subject.table, warning.subject.column) == (
        "transaction",
        "direction",
    )
    assert warning.recurs
    assert "ferro migrate new" in str(warning)
    if db.backend == "postgres":
        assert "migrate_destructive=True" in str(warning)

    report = reconnect(db, destructive=True)

    if db.backend == "sqlite":
        sqlite_warns_and_runs_nothing(report)
        return
    assert schema_sql(report) == [
        'ALTER TABLE "transaction" DROP COLUMN "direction"',
        ADD_DIRECTION,
        INDEX_DIRECTION,
    ]
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db, destructive=True)) == []


def test_migrate_updates_drops_and_adds_back_around_an_input_type_change(
    project, pkg, db
):
    built(project, pkg, db, INDEXED)
    declare(project, pkg, WIDE)

    report = reconnect(db)

    if db.backend == "sqlite":
        assert schema_sql(report) == []
        assert {warning.kind for warning in report.warnings} == {"SqliteInPlace"}
        assert all("ferro migrate new" in str(w) for w in report.warnings)
        assert directions(db) == [(-5, -1)]
        return
    assert schema_sql(report) == [
        'ALTER TABLE "transaction" DROP COLUMN "direction"',
        'ALTER TABLE "transaction" ALTER COLUMN "amount_minor" TYPE bigint '
        'USING "amount_minor"::bigint',
        'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
        'GENERATED ALWAYS AS (CAST(sign("amount_minor") AS bigint)) STORED',
        INDEX_DIRECTION,
    ]
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db)) == []


def test_a_table_check_over_the_column_still_refuses_after_an_input_type_change(
    project, pkg, db
):
    if db.backend == "sqlite":
        pytest.skip("the SQLite pass runs no DDL for the change")
    built(project, pkg, db, CHECKED)
    declare(project, pkg, CHECKED_WIDE)

    report = reconnect(db)

    statements = schema_sql(report)
    assert statements[0] == 'ALTER TABLE "transaction" DROP COLUMN "direction"'
    assert '"ck_transaction_nonzero"' in statements[-1], statements
    with pytest.raises(Exception, match="(?i)check"):
        db.execute(
            'INSERT INTO "transaction" ("amount_minor", "memo") VALUES (0, \'x\')'
        )
    assert schema_sql(reconnect(db)) == []


def test_every_generated_column_statement_is_in_the_statement_log(
    project, pkg, db, caplog
):
    if db.backend == "sqlite":
        pytest.skip("the SQLite pass runs no DDL for a generated column")
    built(project, pkg, db, HEAD)
    declare(project, pkg, INDEXED)
    caplog.set_level(logging.DEBUG, logger="ferro.sql")

    report = reconnect(db)

    logged = [record.sql for record in caplog.records if record.name == "ferro.sql"]
    for statement in schema_sql(report):
        assert statement in logged


def postgres_only(db) -> None:
    if db.backend == "sqlite":
        pytest.skip("the SQLite pass runs no DDL for a generated column")


def positions(statements: list[str], *needles: str) -> list[int]:
    return [
        next(i for i, statement in enumerate(statements) if needle in statement)
        for needle in needles
    ]


CODE_INT = """
from ferro import Field, fn


class Ticket(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    code: int
    n: int = Field(generated=lambda ticket: ticket.code + 1)
"""

CODE_STR = """
from ferro import Field, fn


class Ticket(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    code: str
    n: int = Field(generated=lambda ticket: fn.length(ticket.code))
"""
"""``CODE_INT`` with the input's type and the expression changed together:
the new expression does not type-check against the live table."""


def test_an_input_type_and_expression_changed_together_are_replaced(project, pkg, db):
    postgres_only(db)
    declare(project, pkg, CODE_INT)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    db.execute('INSERT INTO "ticket" ("code") VALUES (1234)')
    declare(project, pkg, CODE_STR)

    # The live read asks Postgres to print no expression it cannot type-check.
    statements = schema_sql(reconnect(db, destructive=True))

    drop, alter, add = positions(
        statements,
        'DROP COLUMN "n"',
        'ALTER COLUMN "code" TYPE',
        'ADD COLUMN "n"',
    )
    assert drop < alter < add, statements
    assert db.rows('SELECT "code", "n" FROM "ticket"') == [("1234", 4)]
    assert schema_sql(reconnect(db, destructive=True)) == []


def test_a_generated_column_added_with_its_input_runs_after_it(project, pkg, db):
    postgres_only(db)
    built(project, pkg, db, HEAD)
    declare(project, pkg, FEE)

    statements = schema_sql(reconnect(db))

    fee, direction = positions(
        statements, 'ADD COLUMN "fee_minor"', 'ADD COLUMN "direction"'
    )
    assert fee < direction, statements
    assert schema_sql(reconnect(db)) == []


def test_a_generated_columns_own_type_change_drops_and_adds_it_back(project, pkg, db):
    postgres_only(db)
    built(project, pkg, db, INDEXED)
    declare(project, pkg, OWN_WIDE)

    statements = schema_sql(reconnect(db))

    assert not any("USING" in statement for statement in statements), statements
    drop, add = positions(
        statements, 'DROP COLUMN "direction"', 'ADD COLUMN "direction"'
    )
    assert drop < add and '"direction" bigint' in statements[add], statements
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db)) == []


def test_a_policy_over_a_replaced_generated_column_is_carried_around_it(
    project, pkg, db
):
    postgres_only(db)
    built(project, pkg, db, POLICED)
    declare(project, pkg, POLICED_WIDE)

    statements = schema_sql(reconnect(db))

    at = positions(
        statements,
        'DROP POLICY "rls_transaction_direction"',
        'DROP COLUMN "direction"',
        'ADD COLUMN "direction"',
        'CREATE POLICY "rls_transaction_direction"',
    )
    assert at == sorted(at), statements
    assert directions(db) == [(-5, -1)]
    assert schema_sql(reconnect(db)) == []


def test_a_raw_policy_over_a_replaced_generated_column_refuses_naming_the_fix(
    project, pkg, db
):
    postgres_only(db)
    built(project, pkg, db, RAW_POLICED)
    declare(project, pkg, RAW_POLICED_WIDE)

    with pytest.raises(Exception) as refused:
        reconnect(db)

    assert 'DROP POLICY "rls_transaction_signed" ON "transaction"' in str(refused.value)
    # Nothing ran: the column and its input stand as they were.
    assert directions(db) == [(-5, -1)]


TEAM = """
from ferro import Check


class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    size: int
    __ferro_checks__ = (Check("size_ok", lambda team: team.size >= 0),)
"""
WIDE_TEAM = TEAM.replace(
    "    size: int\n", '    size: Annotated[int, FerroField(db_type="bigint")]\n'
)


def test_a_check_over_a_retyped_column_is_carried_around_the_alter(project, pkg, db):
    """The live read cannot compare a check over a column whose type changes
    (no printed pair), and Postgres refuses the alter under an old body that
    no longer type-checks: the pass drops the check, alters, and adds the
    declared one back."""
    postgres_only(db)
    declare(project, pkg, TEAM)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    db.execute('INSERT INTO "team" ("size") VALUES (3)')
    declare(project, pkg, WIDE_TEAM)

    statements = schema_sql(reconnect(db))

    drop, alter, add = positions(
        statements,
        'DROP CONSTRAINT "ck_team_size_ok"',
        'ALTER COLUMN "size" TYPE bigint',
        'ADD CONSTRAINT "ck_team_size_ok"',
    )
    assert drop < alter < add, statements
    with pytest.raises(Exception, match="(?i)check"):
        db.execute('INSERT INTO "team" ("size") VALUES (-1)')
    assert schema_sql(reconnect(db)) == []


def test_a_check_changed_with_the_type_of_the_column_it_reads_is_carried(
    project, pkg, db
):
    """``code > 0`` over an integer, then ``length(code) > 0`` over text: the
    alter under the old body would fail (``character varying > integer``)."""
    postgres_only(db)
    declare(project, pkg, CHECKED_CODE)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    db.execute('INSERT INTO "ticket" ("code") VALUES (12)')
    declare(project, pkg, CHECKED_CODE_TEXT)

    statements = schema_sql(reconnect(db))

    drop, alter, add = positions(
        statements,
        'DROP CONSTRAINT "ck_ticket_coded"',
        'ALTER COLUMN "code" TYPE',
        'ADD CONSTRAINT "ck_ticket_coded"',
    )
    assert drop < alter < add, statements
    with pytest.raises(Exception, match="(?i)check"):
        db.execute('INSERT INTO "ticket" ("code") VALUES (\'\')')
    assert schema_sql(reconnect(db)) == []
