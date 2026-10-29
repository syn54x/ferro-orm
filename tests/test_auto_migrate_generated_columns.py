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


EVENT = """
from datetime import datetime

from ferro import Check


class Event(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    starts: Annotated[datetime, FerroField(db_type="timestamp")]
    ends: Annotated[datetime, FerroField(db_type="timestamp")]
    __ferro_checks__ = (Check("ordered", lambda event: event.ends >= event.starts),)
"""
EVENT_TZ = EVENT.replace(
    'Annotated[datetime, FerroField(db_type="timestamp")]', "datetime"
)
"""``EVENT`` with its columns plain ``datetime`` (``timestamptz``), a
conversion the pass refuses: it warns and alters nothing."""


def test_a_check_over_a_column_whose_type_change_is_refused_stays_standing(
    project, pkg, db
):
    """The check is never dropped and added back around an alter that never
    runs, on this pass or the next."""
    postgres_only(db)
    declare(project, pkg, EVENT)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, EVENT_TZ)

    for _ in range(2):
        report = reconnect(db)

        assert schema_sql(report) == []
        assert [warning.kind for warning in report.warnings] == [
            "RefusedConversion",
            "RefusedConversion",
        ], report.warnings


ECHOED = """
from datetime import datetime

from ferro import Field


class Event(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    starts: Annotated[datetime, FerroField(db_type="timestamp")]
    echo: datetime = Field(generated=lambda event: event.starts, db_type="timestamp")
"""
ECHOED_TZ = ECHOED.replace(
    'Annotated[datetime, FerroField(db_type="timestamp")]', "datetime"
).replace(', db_type="timestamp")', ")")
"""``ECHOED`` with ``starts`` and its generated ``echo`` plain ``datetime``:
both conversions are refused, so ``echo`` is never dropped and added back."""


def test_a_generated_column_over_an_input_whose_type_change_is_refused_stays(
    project, pkg, db
):
    postgres_only(db)
    declare(project, pkg, ECHOED)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, ECHOED_TZ)

    for _ in range(2):
        report = reconnect(db, destructive=True)

        assert schema_sql(report) == []
        assert [warning.kind for warning in report.warnings] == [
            "RefusedConversion",
            "RefusedConversion",
        ], report.warnings


DIARY = """
import enum

from ferro import Check


class Mood(str, enum.Enum):
    calm = "calm"
    loud = "loud"


class Diary(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    mood: Annotated[Mood | None, FerroField(db_type="text")] = None
    __ferro_checks__ = (Check("felt", lambda diary: diary.mood != None),)
"""
NATIVE_DIARY = DIARY.replace(
    'Annotated[Mood | None, FerroField(db_type="text")]', "Mood | None"
)
"""``DIARY`` with ``mood`` moved to a native enum type, which no statement
converts in place: the pass reports the recipe and runs nothing."""


def test_a_check_over_a_column_moving_to_an_enum_type_stays_standing(project, pkg, db):
    postgres_only(db)
    declare(project, pkg, DIARY)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, NATIVE_DIARY)

    for _ in range(2):
        report = reconnect(db)

        assert schema_sql(report) == []
        assert [warning.kind for warning in report.warnings] == ["EnumTypeMove"], (
            report.warnings
        )


BADGE = """
from ferro import Check


class Badge(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    __ferro_checks__ = (Check("positive", lambda badge: badge.id > 0),)
"""
NARROW_BADGE = BADGE.replace(
    "FerroField(primary_key=True)", 'FerroField(primary_key=True, db_type="int")'
)
"""``BADGE`` with its key column's type changed: the primary-key recipe's
change, which no door runs in place."""


def test_a_check_over_a_key_column_whose_type_changes_stays_standing(project, pkg, db):
    postgres_only(db)
    declare(project, pkg, BADGE)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, NARROW_BADGE)

    for _ in range(2):
        report = reconnect(db)

        assert schema_sql(report) == []
        assert list(report.warnings) == [], report.warnings


def keyed_ticket(code: str, body: str) -> str:
    """``Ticket`` keyed by ``code`` (annotated ``code``), with ``body`` after it."""
    return (
        "from ferro import Check, Field, RowPolicy, RowSecurity, fn\n\n\n"
        "class Ticket(Model):\n"
        f"    code: Annotated[{code}, FerroField(primary_key=True)]\n" + body
    )


KEYED_TWINS = {
    "check": (
        keyed_ticket(
            "int",
            '    __ferro_checks__ = (Check("coded", lambda ticket: ticket.code > 0),)\n',
        ),
        keyed_ticket(
            "str",
            "    __ferro_checks__ = (\n"
            '        Check("coded", lambda ticket: fn.length(ticket.code) > 0),\n'
            "    )\n",
        ),
    ),
    "generated_column": (
        keyed_ticket(
            "int", "    n: int = Field(generated=lambda ticket: ticket.code + 1)\n"
        ),
        keyed_ticket(
            "str",
            "    n: int = Field(generated=lambda ticket: fn.length(ticket.code))\n",
        ),
    ),
    "policy": tuple(
        keyed_ticket(
            code,
            '    __ferro_rls__ = RowSecurity(RowPolicy(column="code", setting="app.code"))\n',
        )
        for code in ("int", "str")
    ),
}
"""A primary-key ``code`` moved from ``int`` to ``str``, the primary-key
recipe's change no door runs in place, with each kind of artifact over it
declared in a body that types only under its own side's ``code``."""


@pytest.mark.parametrize("kind", list(KEYED_TWINS))
def test_what_reads_a_key_column_whose_type_change_is_withheld_runs_nothing(
    project, pkg, db, kind
):
    """The live read cannot print ``length(code)`` against the live integer
    ``code`` (``function length(integer) does not exist``), so it asks no
    pair for it, and the pass makes no body decision for it: no DDL and no
    error, on every pass."""
    postgres_only(db)
    before, after = KEYED_TWINS[kind]
    declare(project, pkg, before)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, after)

    for _ in range(2):
        report = reconnect(db, destructive=True)

        assert schema_sql(report) == []
        assert_key_refusal_reported(report)


def assert_key_refusal_reported(report: PassReport) -> None:
    """The pass names the primary-key recipe for ``code``'s withheld change."""
    [kept] = [w for w in report.warnings if w.kind == "PrimaryKeyKept"]
    assert '"ticket"."code"' in kept.text, kept.text
    assert "ferro migrate new --data-step" in kept.text, kept.text


QTY_INT = "    qty: int\n"
QTY_BIGINT = '    qty: Annotated[int, FerroField(db_type="bigint")]\n'

MIXED_TWINS = {
    "running_check": (
        keyed_ticket(
            "int",
            QTY_INT
            + "    __ferro_checks__ = (\n"
            + '        Check("both", lambda ticket: ticket.qty + ticket.code > 0),\n'
            + "    )\n",
        ),
        keyed_ticket(
            "str",
            QTY_BIGINT
            + "    __ferro_checks__ = (\n"
            + '        Check("both", lambda ticket: ticket.qty + fn.length(ticket.code) > 0),\n'
            + "    )\n",
        ),
    ),
    "running_generated": (
        keyed_ticket(
            "int",
            QTY_INT
            + "    n: int = Field(generated=lambda ticket: ticket.qty + ticket.code)\n",
        ),
        keyed_ticket(
            "str",
            QTY_BIGINT
            + "    n: int = Field(\n"
            + "        generated=lambda ticket: ticket.qty + fn.length(ticket.code)\n"
            + "    )\n",
        ),
    ),
    "replaced_check": (
        keyed_ticket(
            "int",
            QTY_INT
            + "    total: int = Field(generated=lambda ticket: ticket.qty + 1)\n"
            + "    __ferro_checks__ = (\n"
            + '        Check("both", lambda ticket: ticket.total + ticket.code > 0),\n'
            + "    )\n",
        ),
        keyed_ticket(
            "str",
            QTY_BIGINT
            + "    total: int = Field(generated=lambda ticket: ticket.qty + 1)\n"
            + "    __ferro_checks__ = (\n"
            + '        Check("both", lambda ticket: ticket.total + fn.length(ticket.code) > 0),\n'
            + "    )\n",
        ),
    ),
}
"""A withheld key change of ``code`` beside ``qty``'s widening, which runs,
with an artifact over both: a check over ``qty`` (carried around a running
change), a generated column over ``qty`` (which Postgres refuses to alter
under), and a check over a generated column replaced around ``qty``'s
change (its drop takes the check)."""


def test_a_check_over_a_running_and_a_withheld_column_stays_and_the_rest_runs(
    project, pkg, db
):
    """``qty`` widens; ``ck_ticket_both`` reads it and the withheld key
    ``code``. The check is held, never dropped and added back with a body
    Postgres cannot type against the integer ``code``: only ``qty``'s alter
    runs, and the key's refusal is reported."""
    postgres_only(db)
    before, after = MIXED_TWINS["running_check"]
    declare(project, pkg, before)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, after)

    report = reconnect(db, destructive=True)
    assert schema_sql(report) == [
        'ALTER TABLE "ticket" ALTER COLUMN "qty" TYPE bigint USING "qty"::bigint'
    ]
    assert_key_refusal_reported(report)
    report = reconnect(db, destructive=True)
    assert schema_sql(report) == []
    assert_key_refusal_reported(report)


@pytest.mark.parametrize(
    ("kind", "artifact"),
    [
        ("running_generated", 'generated column "n"'),
        ("replaced_check", 'check "ck_ticket_both"'),
    ],
)
def test_a_change_that_would_take_down_a_held_artifact_is_refused_naming_the_key(
    project, pkg, db, kind, artifact
):
    """``qty``'s widening would alter it under ``n`` (or drop ``total``, and
    ``ck_ticket_both`` with it), and the artifact cannot come back against
    the integer ``code``: the pass refuses, naming the key's recipe, and runs
    nothing."""
    postgres_only(db)
    before, after = MIXED_TWINS[kind]
    declare(project, pkg, before)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()
    declare(project, pkg, after)

    with pytest.raises(Exception) as refused:
        reconnect(db, destructive=True)

    message = str(refused.value)
    assert artifact in message, message
    assert 'resolve the type change of "code" first' in message, message
    assert "ferro migrate new --data-step" in message, message
    assert db.rows(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'ticket' AND column_name = 'qty' "
        f"AND table_schema = '{db.schema}'"
    ) == [("integer",)]
