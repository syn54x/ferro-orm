# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A hand-edited generated expression is drift (ADR-0060).

```python
class Transaction(Model):
    amount_minor: int
    direction: int = Field(generated=lambda transaction: fn.sign(transaction.amount_minor))
```

Postgres stores ``CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)`` and prints it back
as ``(sign(((amount_minor)::bigint)::double precision))::bigint``. Drift asks Postgres
to print the declared rendering beside that reprint in one read-only
``EXPLAIN``, so the reprint is silent and a hand edit is one line::

    transaction.direction generated expression differs

SQLite stores the clause as written, so its text is compared with ferro's
rendering, whitespace folded.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest

import ferro
from ferro.migrations import MigrationRefused, baseline
from tests._pass_harness import auto_migrate, schema_sql
from tests.test_auto_migrate_generated_columns import KEYED_TWINS, MIXED_TWINS
from tests.test_migrate_drift import drift_api
from tests.test_migrate_new import (  # noqa: F401 - fixtures
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
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

LEDGER = """
from ferro import Field, fn


class Transaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount_minor: int
    memo: str
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.amount_minor)
    )
"""

LINE = "transaction.direction generated expression differs"


def migrated(project, pkg, db, capsys) -> None:
    """``0001_create_ledger`` applied, with one row."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, LEDGER)
    new("create_ledger")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()
    db.execute(
        'INSERT INTO "transaction" ("amount_minor", "memo") VALUES (-5, \'refund\')'
    )


def edit_by_hand(db, old: str, new: str) -> None:
    """Rewrite ``direction``'s expression by hand: in place on Postgres, a
    hand-written table rebuild on SQLite."""
    if db.backend == "postgres":
        db.execute(
            'ALTER TABLE "transaction" ALTER COLUMN "direction" '
            f'SET EXPRESSION AS ({new}("amount_minor"))'
        )
        return
    [(create,)] = db.rows(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transaction'"
    )
    db.execute('ALTER TABLE "transaction" RENAME TO "transaction_old"')
    db.execute(create.replace(f"{old}(", f"{new}("))
    db.execute(
        'INSERT INTO "transaction" ("id", "amount_minor", "memo") '
        'SELECT "id", "amount_minor", "memo" FROM "transaction_old"'
    )
    db.execute('DROP TABLE "transaction_old"')


# -- drift ---------------------------------------------------------------------------


def test_a_hand_edited_expression_is_one_drift_line(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys)
    edit_by_hand(db, "sign", "abs")

    report = drift_api(db)

    assert report.lines == [LINE]
    assert run("migrate", "drift", "--url", db.url) == 4
    assert f"  - {LINE}\n" in capsys.readouterr().out


def auto_migrated(project, pkg, db, body: str = LEDGER) -> None:
    """The models of ``body`` built by ``connect(auto_migrate=True)``."""
    import importlib
    import sys

    write_models(project, pkg, body)
    sys.path.insert(0, str(project))
    importlib.import_module(f"{pkg}.models")
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()


def reconnect_statements(db) -> list[str]:
    """What ``connect(migrate_updates=True, migrate_destructive=True)`` runs."""
    report = asyncio.run(auto_migrate(db.url, updates=True, destructive=True))
    ferro.reset_engine()
    assert list(report.warnings) == []
    return schema_sql(report)


def test_a_reprint_is_silent_and_a_reconnect_runs_nothing(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys)
    if db.backend == "postgres":
        assert db.rows(
            "SELECT generation_expression FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND column_name = 'direction'"
        ) == [("(sign(((amount_minor)::bigint)::double precision))::bigint",)], (
            "the pin is only meaningful if Postgres reprints the expression"
        )

    assert drift_api(db).lines == []


def test_a_reconnect_runs_nothing_for_an_unchanged_generated_column(project, pkg, db):
    auto_migrated(project, pkg, db)
    assert reconnect_statements(db) == []


def test_a_declared_generated_column_absent_live_is_missing(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys)
    db.execute('ALTER TABLE "transaction" DROP COLUMN "direction"')

    assert drift_api(db).lines == ["transaction.direction column is missing"]


def test_a_plain_column_where_the_model_declares_a_generated_one_differs(
    project, pkg, db, capsys
):
    migrated(project, pkg, db, capsys)
    db.execute('ALTER TABLE "transaction" DROP COLUMN "direction"')
    if db.backend == "postgres":
        db.execute('ALTER TABLE "transaction" ADD COLUMN "direction" integer')
        db.execute('UPDATE "transaction" SET "direction" = -1')
        db.execute('ALTER TABLE "transaction" ALTER COLUMN "direction" SET NOT NULL')
    else:
        db.execute(
            'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL DEFAULT -1'
        )

    assert drift_api(db).lines == [LINE]


def test_a_virtual_column_differs_from_the_stored_one(project, pkg, db, capsys):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot add a stored column in place to compare with")
    [(version_num,)] = db.rows("SHOW server_version_num")
    if int(version_num) < 180000:
        pytest.skip("VIRTUAL generated columns need PostgreSQL 18")
    migrated(project, pkg, db, capsys)
    db.execute('ALTER TABLE "transaction" DROP COLUMN "direction"')
    db.execute(
        'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
        'GENERATED ALWAYS AS (CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)) VIRTUAL'
    )

    assert drift_api(db).lines == [LINE]


# -- baseline ------------------------------------------------------------------------


def test_baseline_refuses_a_hand_edited_expression_on_the_drift_line(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, LEDGER)
    new("create_ledger")
    auto_migrated(project, pkg, db)
    edit_by_hand(db, "sign", "abs")

    report = asyncio.run(baseline(url=db.url))

    assert report.recorded == []
    assert report.drift is not None and report.drift.lines == [LINE]
    with pytest.raises(MigrationRefused, match=LINE):
        report.raise_for_problems()


# -- a rename in the same change -----------------------------------------------------

RENAMED = """
from ferro import Field, fn


class Transaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: Annotated[int, FerroField(renamed_from="amount_minor")]
    memo: str
    direction: int = Field(generated=lambda transaction: fn.sign(transaction.amount))
"""


def test_a_renamed_input_column_reads_the_live_names_and_is_the_rename_alone(
    project, pkg, db, capsys
):
    """The live read renders the declared ``sign("amount")`` under the live
    name ``amount_minor`` for Postgres to print, so the ``EXPLAIN`` does not
    fail, and the pass renames the column and nothing else."""
    auto_migrated(project, pkg, db)
    write_models(project, pkg, RENAMED)
    import importlib

    importlib.import_module(f"{pkg}.models")

    assert reconnect_statements(db) == [
        'ALTER TABLE "transaction" RENAME COLUMN "amount_minor" TO "amount"'
    ]
    assert reconnect_statements(db) == []


# -- a SQLite rebuild ----------------------------------------------------------------

OPTIONAL_MEMO = LEDGER.replace("memo: str", "memo: str | None = None")


def test_a_sqlite_rebuild_copies_no_generated_column_and_recomputes_it(
    project, pkg, db, capsys
):
    if db.backend != "sqlite":
        pytest.skip("Postgres changes nullability in place")
    migrated(project, pkg, db, capsys)
    write_models(project, pkg, OPTIONAL_MEMO)
    new("optional_memo")
    step = next(migrations(project).glob("0002_optional_memo/*.up.sqlite.sql"))
    copy = next(
        line for line in step.read_text().splitlines() if line.startswith("INSERT")
    )
    assert '"direction"' not in copy, copy

    assert run("migrate", "up", "--url", db.url) == 0
    assert db.rows('SELECT "amount_minor", "direction" FROM "transaction"') == [
        (-5, -1)
    ]
    assert drift_api(db).lines == []


# -- privileges (Postgres) -----------------------------------------------------------


@contextlib.contextmanager
def reader(db, pg_role, *, select_transaction: bool) -> Iterator[str]:
    """A login role that reads the schema in read-only transactions, with or
    without ``SELECT`` on ``transaction``; yields its URL."""
    role = pg_role("drift")
    try:
        db.execute(f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER PASSWORD 'drift-pw'")
        db.execute(f'ALTER ROLE "{role}" SET default_transaction_read_only = on')
        db.execute(f'GRANT USAGE ON SCHEMA "{db.schema}" TO "{role}"')
        for (table,) in db.rows(
            "SELECT tablename FROM pg_tables "
            f"WHERE schemaname = '{db.schema}' AND tablename <> 'transaction'"
        ):
            db.execute(f'GRANT SELECT ON "{table}" TO "{role}"')
        if select_transaction:
            db.execute(f'GRANT SELECT ON "transaction" TO "{role}"')
        parsed = urlparse(db.url)
        netloc = f"{role}:drift-pw@{parsed.hostname}:{parsed.port}"
        yield parsed._replace(netloc=netloc).geturl()
    finally:
        db.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE usename = '{role}' AND pid <> pg_backend_pid()"
        )
        with contextlib.suppress(Exception):
            db.execute(f'DROP OWNED BY "{role}"')
        db.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_drift_works_under_a_select_only_read_only_role(
    project, pkg, db, capsys, pg_role
):
    if db.backend != "postgres":
        pytest.skip("roles are Postgres's")
    migrated(project, pkg, db, capsys)
    edit_by_hand(db, "sign", "abs")

    with reader(db, pg_role, select_transaction=True) as url:
        report = asyncio.run(ferro.migrations.drift(url=url))

    assert (report.refusal, report.lines) == (None, [LINE])


def test_drift_without_select_is_refused_naming_the_grant(
    project, pkg, db, capsys, pg_role
):
    if db.backend != "postgres":
        pytest.skip("roles are Postgres's")
    migrated(project, pkg, db, capsys)

    with reader(db, pg_role, select_transaction=False) as url:
        with pytest.raises(MigrationRefused, match='GRANT SELECT ON "transaction"'):
            asyncio.run(ferro.migrations.drift(url=url))


# -- an input added in the same change -----------------------------------------------

FEE = """
from ferro import Field, fn


class Transaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount_minor: int
    memo: str
    fee_minor: int = 0
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.fee_minor)
    )
"""


def test_an_expression_over_a_column_the_same_change_adds_is_planned_not_refused(
    project, pkg, db
):
    """``direction`` moves to ``sign(fee_minor)`` while ``fee_minor`` is
    added: the live table has no ``fee_minor`` to print a pair under, so the
    live read prints none and none is demanded. Drift reports the missing
    column and the changed expression; on Postgres the pass adds the column,
    sets the expression, and plans nothing on the next connect."""
    import importlib

    from ferro.ir import compile_registry_schema_ir
    from ferro.migrations._drift import against

    auto_migrated(project, pkg, db)
    write_models(project, pkg, FEE)
    importlib.import_module(f"{pkg}.models")

    async def drift_lines() -> list[str]:
        await ferro.connect(db.url, name="drift_fee")
        try:
            report = await against(
                compile_registry_schema_ir(), migration="0002_fee", using="drift_fee"
            )
        finally:
            ferro.reset_engine()
        return report.lines

    assert asyncio.run(drift_lines()) == [
        "transaction.fee_minor column is missing",
        LINE,
    ]
    if db.backend != "postgres":
        return
    statements = reconnect_statements(db)
    assert statements[-1] == (
        'ALTER TABLE "transaction" ALTER COLUMN "direction" '
        'SET EXPRESSION AS (CAST(sign(CAST("fee_minor" AS bigint)) AS bigint))'
    ), statements
    assert any('ADD COLUMN "fee_minor"' in s for s in statements), statements
    assert reconnect_statements(db) == []
    assert asyncio.run(drift_lines()) == []


# -- a generated column dropped and added back, read by the planner's reason -------


def test_an_input_retyped_by_hand_with_its_expression_is_one_type_line(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("the hand edit is Postgres's")
    migrated(project, pkg, db, capsys)
    # The input's type and the expression changed together: the declared
    # expression does not type-check against the live table, so drift asks
    # Postgres to print none of it.
    db.execute('ALTER TABLE "transaction" DROP COLUMN "direction"')
    db.execute(
        'ALTER TABLE "transaction" ALTER COLUMN "amount_minor" TYPE text '
        'USING "amount_minor"::text'
    )
    db.execute(
        'ALTER TABLE "transaction" ADD COLUMN "direction" integer NOT NULL '
        'GENERATED ALWAYS AS (length("amount_minor")) STORED'
    )

    report = drift_api(db)

    # The column is dropped and added back around its input's type change,
    # whose own line says it.
    [line] = report.lines
    assert line.startswith("transaction.amount_minor has type text"), line
    replaced = [op for op in report.operations if op.get("replaces")]
    assert {op["replaces"] for op in replaced} == {"input_type"}, replaced


def test_a_generated_column_retyped_by_hand_is_one_type_line(project, pkg, db, capsys):
    if db.backend != "postgres":
        pytest.skip("the hand edit is Postgres's")
    migrated(project, pkg, db, capsys)
    db.execute('ALTER TABLE "transaction" ALTER COLUMN "direction" TYPE bigint')

    report = drift_api(db)

    assert report.lines == ["transaction.direction has type bigint, snapshot says int"]
    assert {op["kind"]: op["replaces"] for op in report.operations} == {
        "DropColumn": "own_type",
        "AddColumn": "own_type",
    }


RETYPE_KEY = (
    'ALTER TABLE "ticket" ALTER COLUMN "code" TYPE integer USING length("code")'
)


def key_retyped_by_hand(db, kind: str) -> None:
    """``code`` put back to ``integer`` by hand, with the artifact over it in
    the body that types under an integer ``code``."""
    if kind == "check":
        db.execute('ALTER TABLE "ticket" DROP CONSTRAINT "ck_ticket_coded"')
        db.execute(RETYPE_KEY)
        db.execute(
            'ALTER TABLE "ticket" ADD CONSTRAINT "ck_ticket_coded" CHECK ("code" > 0)'
        )
    elif kind == "generated_column":
        db.execute('ALTER TABLE "ticket" DROP COLUMN "n"')
        db.execute(RETYPE_KEY)
        db.execute(
            'ALTER TABLE "ticket" ADD COLUMN "n" integer NOT NULL '
            'GENERATED ALWAYS AS ("code" + 1) STORED'
        )
    else:
        [(name,)] = db.rows(
            "SELECT policyname FROM pg_policies WHERE tablename = 'ticket'"
        )
        db.execute(f'DROP POLICY "{name}" ON "ticket"')
        db.execute(RETYPE_KEY)
        db.execute(
            f'CREATE POLICY "{name}" ON "ticket" USING '
            "(\"code\" = current_setting('app.code')::integer)"
        )


@pytest.mark.parametrize("kind", list(KEYED_TWINS))
def test_what_reads_a_key_column_whose_type_change_is_withheld_is_not_compared(
    project, pkg, db, capsys, kind
):
    """The snapshot keys ``ticket`` by a text ``code``; the database by hand
    holds an integer one, with the check, generated column or policy over it
    written for an integer. The type change is the primary-key recipe's, so
    no emitter runs it, and the artifact cannot be compared before it does:
    drift names it after the type line, and the read prints no pair for it
    (no ``function length(integer) does not exist``)."""
    if db.backend != "postgres":
        pytest.skip("the hand edit is Postgres's")
    configure(project, pkg, db.backend)
    write_models(project, pkg, KEYED_TWINS[kind][1])
    new("create_ticket")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()
    key_retyped_by_hand(db, kind)

    report = drift_api(db)

    [held] = [op for op in report.operations if op.get("held")]
    assert (held["kind"], held["column"]) == ("AlterColumnType", "code")
    assert [artifact["kind"] for artifact in held["held"]["artifacts"]] == [kind]
    [artifact] = held["held"]["artifacts"]
    subject = {
        "check": "ck_ticket_coded check",
        "generated_column": "ticket.n generated expression",
    }.get(kind, f"{artifact['name']} policy")
    noun = "input's" if kind == "generated_column" else "column's"
    type_line, held_line = report.lines
    assert type_line == "ticket.code has type int, snapshot says varchar"
    assert held_line.startswith(
        f"{subject} not compared while its {noun} type change is withheld; "
        'resolve the type change of "code" first: '
    ), held_line
    assert "ferro migrate new --data-step" in held_line, held_line


def mixed_retyped_by_hand(db, kind: str) -> None:
    """``qty`` back to ``integer`` and ``code`` to an integer key by hand,
    with the artifact over both written for them."""
    retype = (
        'ALTER TABLE "ticket" ALTER COLUMN "qty" TYPE integer',
        RETYPE_KEY,
    )
    if kind == "running_check":
        db.execute('ALTER TABLE "ticket" DROP CONSTRAINT "ck_ticket_both"')
        for statement in retype:
            db.execute(statement)
        db.execute(
            'ALTER TABLE "ticket" ADD CONSTRAINT "ck_ticket_both" '
            'CHECK ("qty" + "code" > 0)'
        )
    elif kind == "running_generated":
        db.execute('ALTER TABLE "ticket" DROP COLUMN "n"')
        for statement in retype:
            db.execute(statement)
        db.execute(
            'ALTER TABLE "ticket" ADD COLUMN "n" integer NOT NULL '
            'GENERATED ALWAYS AS ("qty" + "code") STORED'
        )
    else:
        db.execute('ALTER TABLE "ticket" DROP CONSTRAINT "ck_ticket_both"')
        db.execute('ALTER TABLE "ticket" DROP COLUMN "total"')
        for statement in retype:
            db.execute(statement)
        db.execute(
            'ALTER TABLE "ticket" ADD COLUMN "total" integer NOT NULL '
            'GENERATED ALWAYS AS ("qty" + 1) STORED'
        )
        db.execute(
            'ALTER TABLE "ticket" ADD CONSTRAINT "ck_ticket_both" '
            'CHECK ("total" + "code" > 0)'
        )


@pytest.mark.parametrize("kind", list(MIXED_TWINS))
def test_an_artifact_over_a_running_and_a_withheld_column_is_held_on_the_drift_line(
    project, pkg, db, capsys, kind
):
    """``qty`` and the key ``code`` both differ from the snapshot; the check
    or generated column reads both. Drift reads without asking a pair for it
    and names it held, with the key's fix, after the type lines."""
    if db.backend != "postgres":
        pytest.skip("the hand edit is Postgres's")
    configure(project, pkg, db.backend)
    write_models(project, pkg, MIXED_TWINS[kind][1])
    new("create_ticket")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()
    mixed_retyped_by_hand(db, kind)

    report = drift_api(db)

    subject = (
        "ticket.n generated expression not compared while its input's"
        if kind == "running_generated"
        else "ck_ticket_both check not compared while its column's"
    )
    held = [line for line in report.lines if "withheld" in line]
    assert held == [
        f"{subject} type change is withheld; "
        'resolve the type change of "code" first: '
        + held[0].split('resolve the type change of "code" first: ', 1)[1]
    ], report.lines
    assert "ferro migrate new --data-step" in held[0], held
    assert "ticket.code has type int, snapshot says varchar" in report.lines
