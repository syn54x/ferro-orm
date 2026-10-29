# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""A table check that calls `ferro.fn` (ADR-0059, ADR-0060).

```python
class Payment(Model):
    __ferro_checks__ = (
        Check("large", lambda payment: fn.abs(payment.amount) > 1000),
    )
    id: int | None = Field(default=None, primary_key=True)
    amount: int
```

```sql
CONSTRAINT "ck_payment_large" CHECK ((abs("amount") > 1000))
```

A comparison whose operand is not a plain column or literal lowers to the
`compare` kind over value expressions, typed under the same value parity as
a generated column's expression. On Postgres the `int` column is widened to
`bigint` inside the call, and Postgres prints the stored body differently
again; drift asks Postgres to print both bodies, so a check ferro created is
silent and a hand edit is one line.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime
from typing import Annotated, ClassVar

import pytest

import ferro
from ferro import Check, CheckViolationError, Field, Model, fn
from ferro._core import _render_create_table_sql_for_test
from ferro.base import FerroField
from ferro.ir import compile_registry_schema_ir
from tests._pass_harness import auto_migrate, schema_sql
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

pytestmark = pytest.mark.usefixtures("clean_registry")


def declare_payment() -> type[Model]:
    class Payment(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("large", lambda payment: fn.abs(payment.amount) > 1000),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int

    return Payment


def declare_annotated_payment() -> type[Model]:
    class Payment(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("large", lambda payment: fn.abs(payment.amount) > 1000),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: Annotated[int, FerroField()]

    return Payment


STYLES = pytest.mark.parametrize(
    "declare",
    [declare_payment, declare_annotated_payment],
    ids=["assignment", "annotated"],
)


def predicate(table: str, suffix: str) -> dict:
    """The lowered predicate of `ck_<table>_<suffix>`."""
    [model] = [
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == table
    ]
    [check] = [c for c in model["table_checks"] if c["name"] == f"ck_{table}_{suffix}"]
    return check["predicate"]


def column(name: str) -> dict:
    return {"kind": "column", "name": name}


def literal(token: str) -> dict:
    return {"kind": "literal", "token": token}


ABS_AMOUNT = {"kind": "func", "name": "abs", "args": [column("amount")]}


# -- lowering ---------------------------------------------------------------------


@STYLES
def test_a_check_calling_fn_lowers_to_a_compare_of_value_expressions(declare):
    declare()
    assert predicate("payment", "large") == {
        "kind": "compare",
        "left": ABS_AMOUNT,
        "op": "gt",
        "right": literal("1000"),
    }


def test_a_literal_on_the_left_compares_the_call_from_the_right():
    class Payment(Model):
        __ferro_checks__ = (
            Check("large", lambda payment: 1000 < fn.abs(payment.amount)),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int

    assert predicate("payment", "large") == {
        "kind": "compare",
        "left": ABS_AMOUNT,
        "op": "gt",
        "right": literal("1000"),
    }


def test_a_column_compared_with_arithmetic_is_a_compare():
    class Payment(Model):
        __ferro_checks__ = (
            Check("covered", lambda payment: payment.limit >= payment.amount * 2),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int
        limit: int

    assert predicate("payment", "covered") == {
        "kind": "compare",
        "left": column("limit"),
        "op": "ge",
        "right": {
            "kind": "binary",
            "op": "mul",
            "left": column("amount"),
            "right": literal("2"),
        },
    }


def test_a_plain_comparison_keeps_its_cmp_bytes():
    class Payment(Model):
        __ferro_checks__ = (
            Check("positive", lambda payment: payment.amount > 0),
            Check("within", lambda payment: payment.amount <= payment.limit),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int
        limit: int

    assert predicate("payment", "positive") == {
        "kind": "cmp",
        "column": "amount",
        "op": "gt",
        "other": literal("0"),
    }
    assert predicate("payment", "within") == {
        "kind": "cmp",
        "column": "amount",
        "op": "le",
        "other": column("limit"),
    }


def test_a_compare_combines_with_and_or_and_not():
    class Payment(Model):
        __ferro_checks__ = (
            Check(
                "shaped",
                lambda payment: ~(fn.abs(payment.amount) > 1000) | (payment.amount > 0),
            ),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int

    assert predicate("payment", "shaped") == {
        "kind": "or",
        "left": {
            "kind": "not",
            "child": {
                "kind": "compare",
                "left": ABS_AMOUNT,
                "op": "gt",
                "right": literal("1000"),
            },
        },
        "right": {"kind": "cmp", "column": "amount", "op": "gt", "other": literal("0")},
    }


# -- value parity ------------------------------------------------------------------


def test_fn_least_over_a_nullable_column_is_refused_naming_coalesce():
    with pytest.raises(TypeError, match=r"wrap it as fn\.coalesce") as caught:

        class Payment(Model):
            __ferro_checks__ = (
                Check("capped", lambda payment: fn.least(payment.cap, 28) > 1),
            )
            id: int | None = Field(default=None, primary_key=True)
            cap: int | None = None

    assert "Payment.__ferro_checks__ check 'capped'" in str(caught.value)
    assert "nullable column 'cap'" in str(caught.value)


def test_float_multiplication_in_a_check_is_refused():
    with pytest.raises(TypeError, match="over a float"):

        class Payment(Model):
            __ferro_checks__ = (
                Check("scaled", lambda payment: payment.rate * 2.5 > 1),
            )
            id: int | None = Field(default=None, primary_key=True)
            rate: float


def test_a_date_part_of_a_datetime_is_refused():
    with pytest.raises(TypeError, match="time zone"):

        class Payment(Model):
            __ferro_checks__ = (
                Check("early", lambda payment: fn.day(payment.paid_at) < 28),
            )
            id: int | None = Field(default=None, primary_key=True)
            paid_at: datetime


def test_operands_of_different_families_are_refused():
    with pytest.raises(
        TypeError, match=r"compares integer \(int\) with string \(str\)"
    ):

        class Payment(Model):
            __ferro_checks__ = (
                Check("odd", lambda payment: fn.length(payment.memo) > "x"),
            )
            id: int | None = Field(default=None, primary_key=True)
            memo: str


def test_a_value_expression_compared_with_none_is_refused():
    with pytest.raises(TypeError, match="IS NULL"):

        class Payment(Model):
            __ferro_checks__ = (
                Check("set", lambda payment: fn.abs(payment.amount) != None),  # noqa: E711
            )
            id: int | None = Field(default=None, primary_key=True)
            amount: int


def test_fn_inside_in_is_refused():
    with pytest.raises(TypeError, match=r"\.in_\(\) takes literals"):

        class Payment(Model):
            __ferro_checks__ = (
                Check(
                    "listed",
                    lambda payment: payment.amount.in_([fn.abs(payment.amount)]),
                ),
            )
            id: int | None = Field(default=None, primary_key=True)
            amount: int


@pytest.mark.parametrize(
    "body",
    [
        lambda payment: fn.length(payment.memo) > b"x",
        lambda payment: fn.abs(payment.amount > 1) > 0,
    ],
    ids=["a bytes literal", "a comparison as an argument"],
)
def test_a_refused_operand_names_the_check_not_a_generated_column(body):
    with pytest.raises(TypeError) as caught:

        class Payment(Model):
            __ferro_checks__ = (Check("odd", body),)
            id: int | None = Field(default=None, primary_key=True)
            amount: int
            memo: str

    assert "Payment.__ferro_checks__ check 'odd'" in str(caught.value)
    assert "generated column" not in str(caught.value)


def test_fn_in_a_query_stays_refused():
    Payment = declare_payment()
    with pytest.raises(TypeError, match=r"ferro\.fn cannot appear"):
        Payment.where(lambda payment: fn.abs(payment.amount) > 1000)


# -- rendering ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "body"),
    [
        ("sqlite", '(abs("amount") > 1000)'),
        ("postgres", '(abs(CAST("amount" AS bigint)) > 1000)'),
    ],
)
def test_the_create_table_renders_the_call_per_dialect(dialect, body):
    declare_payment()
    payload = json.dumps(compile_registry_schema_ir()["payload"])
    create_sql, _post, _pre = _render_create_table_sql_for_test(
        "Payment", payload, dialect
    )
    assert f'CONSTRAINT "ck_payment_large" CHECK ({body})' in create_sql


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@STYLES
async def test_the_database_enforces_the_call(declare, db_url):
    Payment = declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        refund = await Payment.create(amount=-2000)
        assert refund.amount == -2000
        with pytest.raises(CheckViolationError, match="ck_payment_large"):
            await Payment.create(amount=500)


# -- drift and reconnect -----------------------------------------------------------

HEAD = """
from ferro import Check, fn


class Payment(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount_minor: Annotated[int, FerroField(db_type="bigint")]
"""

SIGNED_CHECK = """
    __ferro_checks__ = (
        Check("signed", lambda payment: fn.sign(payment.amount_minor) != 0),
    )
"""

LEDGER = HEAD + SIGNED_CHECK
"""`HEAD` with a table check calling `fn.sign`: the casebook
(`tests/_casebook.py`) adds the check to `HEAD` from these bodies."""

LINE = "ck_payment_signed check body differs"


def migrated(project, pkg, db, capsys) -> None:
    """``0001_create_ledger`` applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, LEDGER)
    new("create_ledger")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()


def auto_migrated(project, pkg, db) -> None:
    """The ledger built by ``connect(auto_migrate=True)``."""
    write_models(project, pkg, LEDGER)
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


def edit_by_hand(db) -> None:
    """Loosen ``ck_payment_signed`` by hand: in place on Postgres, a
    hand-written table rebuild on SQLite."""
    if db.backend == "postgres":
        db.execute('ALTER TABLE "payment" DROP CONSTRAINT "ck_payment_signed"')
        db.execute(
            'ALTER TABLE "payment" ADD CONSTRAINT "ck_payment_signed" '
            'CHECK (sign("amount_minor") > -2)'
        )
        return
    [(create,)] = db.rows(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'payment'"
    )
    db.execute('ALTER TABLE "payment" RENAME TO "payment_old"')
    db.execute(create.replace("<> 0", "> -2"))
    db.execute('DROP TABLE "payment_old"')


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_ferro_created_check_calling_fn_is_not_drift(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys)
    if db.backend == "postgres":
        assert db.rows(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_payment_signed' "
            f"AND connamespace = '{db.schema}'::regnamespace"
        ) == [
            (
                "CHECK ((sign((amount_minor)::double precision) <> (0)::double precision))",
            )
        ], "the pin is only meaningful if Postgres reprints the body"

    report = drift_api(db)
    assert report.lines == [] and report.clean, report.lines


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_reconnect_runs_nothing_for_a_check_calling_fn(project, pkg, db):
    auto_migrated(project, pkg, db)
    assert reconnect_statements(db) == []
    assert reconnect_statements(db) == []


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_hand_edited_check_calling_fn_is_one_drift_line(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys)
    edit_by_hand(db)

    assert drift_api(db).lines == [LINE]


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_reconnect_rebuilds_a_hand_edited_postgres_check(project, pkg, db):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot rebuild a check in place; it warns instead")
    auto_migrated(project, pkg, db)
    edit_by_hand(db)

    assert reconnect_statements(db) == [
        'ALTER TABLE "payment" DROP CONSTRAINT "ck_payment_signed"',
        'ALTER TABLE "payment" ADD CONSTRAINT "ck_payment_signed" '
        'CHECK ((sign("amount_minor") <> 0))',
    ]
    assert reconnect_statements(db) == []
