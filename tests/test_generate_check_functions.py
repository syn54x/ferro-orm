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
from typing import Annotated, Any, ClassVar

import pytest

import ferro
from ferro import Check, CheckViolationError, Field, Model, fn
from ferro._core import _render_create_table_sql_for_test
from ferro.base import FerroField
from ferro.exceptions import DataError
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
                lambda payment: ~(fn.abs(payment.amount) > 1000) & (payment.amount > 0),
            ),
        )
        id: int | None = Field(default=None, primary_key=True)
        amount: int

    assert predicate("payment", "shaped") == {
        "kind": "and",
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


BIG = Annotated[int, FerroField(db_type="bigint")]


def declare_box(body) -> type[Model]:
    class Box(Model):
        __ferro_checks__ = (Check("c", body),)
        id: int | None = Field(default=None, primary_key=True)
        a: BIG = 0
        b: BIG = 0
        x: float = 0.0
        y: float = 0.0
        kind: int = 0

    return Box


def overflow_error() -> Any:
    """An overflow computing the check: Postgres raises it, and SQLite's
    step guard raises `integer overflow`; both are a `DataError` naming the
    check."""
    return pytest.raises(DataError, match='the check "ck_box_c" of "box"')


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "fits", "overflows"),
    [
        (lambda box: box.a * box.b > 0, {"a": 3, "b": 4}, {"a": 2**40, "b": 2**40}),
        (lambda box: box.a + box.b > 0, {"a": 1, "b": 2}, {"a": 2**62, "b": 2**62}),
        (lambda box: -box.a > 0, {"a": -5}, {"a": -(2**63)}),
        (lambda box: box.a / -1 > 0, {"a": -5}, {"a": -(2**63)}),
        (
            lambda box: box.x + box.y > 0,
            {"x": 1.5, "y": 2.5},
            {"x": 1.7e308, "y": 1.7e308},
        ),
        (
            lambda box: box.x - box.y < 0,
            {"x": 1.5, "y": 2.5},
            {"x": -1.7e308, "y": 1.7e308},
        ),
    ],
    ids=[
        "bigint *",
        "bigint +",
        "bigint negated",
        "bigint over -1",
        "float +",
        "float -",
    ],
)
async def test_a_check_step_that_overflows_fails_the_row_on_both_databases(
    db_url, body, fits, overflows
):
    """Postgres raises where a step overflows; SQLite would judge a real or
    `inf`, so its check guards every step and raises on the same row."""
    Box = declare_box(body)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Box.create(**fits)
        with overflow_error():
            await Box.create(**overflows)
        assert len(await Box.all()) == 1


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_step_under_not_still_fails_the_row(db_url):
    """The terms sit outside the body, so `NOT` cannot turn an overflow into
    a pass."""
    Box = declare_box(lambda box: ~(box.x + box.y < 0))
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Box.create(x=1.0, y=2.0)
        with overflow_error():
            await Box.create(x=1.7e308, y=1.7e308)


@pytest.mark.parametrize(
    ("body", "step"),
    [
        (lambda box: (box.kind == 1) | (box.a + box.b > 0), "(a + b)"),
        (lambda box: (box.kind == 1) | (box.a * box.b > 0), "(a * b)"),
        (lambda box: (box.kind == 1) | (-box.a > 0), "-a"),
        (lambda box: (box.kind == 1) | (box.a / -1 > 0), "(a / -1)"),
        (lambda box: (box.kind == 1) | (box.x + box.y > 0), "(x + y)"),
        (lambda box: (box.kind == 1) | (fn.abs(box.a) > 0), "fn.abs(a)"),
        (
            lambda box: ~((box.kind == 1) | ((box.kind == 2) & (box.a - 1 > 0))),
            "(a - 1)",
        ),
    ],
    ids=[
        "int +",
        "int *",
        "int negated",
        "int over -1",
        "float +",
        "abs of an int",
        "nested",
    ],
)
def test_a_step_that_can_overflow_under_an_or_is_refused(body, step):
    """Postgres does not define which side of an OR it evaluates, so it may
    skip a step SQLite's guard raises on; the check is refused instead."""
    with pytest.raises(TypeError) as caught:
        declare_box(body)
    message = str(caught.value)
    assert "Box.__ferro_checks__ check 'c'" in message
    assert f"computes {step} under an OR" in message
    assert "generated column" in message


@pytest.mark.parametrize(
    "body",
    [
        lambda box: (box.kind == 1) | (box.a / 2 > 0),
        lambda box: (box.kind == 1) | (-box.x > 0),
        lambda box: (box.kind == 1) | (fn.abs(box.x) > 0),
        lambda box: (box.kind == 1) | (fn.sign(box.a) > 0),
        lambda box: (box.kind == 1) & (box.a * box.b > 0),
    ],
    ids=["int halved", "float negated", "abs of a float", "sign", "under AND"],
)
def test_a_step_that_cannot_overflow_under_an_or_is_accepted(body):
    declare_box(body)
    assert predicate("box", "c")["kind"] in ("or", "and")


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_abs_of_the_smallest_bigint_is_a_data_error_on_both_databases(db_url):
    """`abs(-2**63)` does not fit a bigint: both databases raise computing
    it, and the write reports the overflow as the check's."""
    Box = declare_box(lambda box: fn.abs(box.a) > 0)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Box.create(a=-5)
        with overflow_error():
            await Box.create(a=-(2**63))
        assert len(await Box.all()) == 1


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_step_beside_a_false_conjunct_fails_the_same_way(db_url):
    """`kind == 2` is false, so neither database reaches `a * b`: the row
    is the check's violation on both. With `kind == 2` the product is
    computed and its overflow is a `DataError` on both."""
    Box = declare_box(lambda box: (box.kind == 2) & (box.a * box.b > 0))
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Box.create(kind=2, a=3, b=4)
        with pytest.raises(CheckViolationError, match="ck_box_c"):
            await Box.create(kind=1, a=2**40, b=2**40)
        with overflow_error():
            await Box.create(kind=2, a=2**40, b=2**40)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_stored_infinite_input_passes_on_both_databases(db_url):
    """Postgres adds an already infinite float without raising, so the
    guard lets an infinite operand through; a fresh overflow still fails."""
    Box = declare_box(lambda box: box.x + box.y > 0)
    await ferro.connect(db_url, auto_migrate=True)
    # Ferro never binds a non-finite float, so the row is written by hand.
    infinity = "9e999" if db_url.startswith("sqlite") else "'Infinity'::float8"
    async with ferro.engines.session():
        written = await ferro.execute(
            'INSERT INTO "box" ("a", "b", "x", "y", "kind") '
            f"VALUES (0, 0, {infinity}, 1.0, 0)"
        )
        assert written == 1
        with overflow_error():
            await Box.create(x=1.7e308, y=1.7e308)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_an_update_that_overflows_a_check_is_a_data_error(db_url):
    """A table with a computing check and no generated column: saving a
    persisted row and an `update()` both report the overflow as the check's."""
    Box = declare_box(lambda box: box.a * box.b > 0)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        row = await Box.create(a=3, b=4)
        row.a = 2**40
        row.b = 2**40
        with overflow_error():
            await row.save()
        with overflow_error():
            await Box.where(lambda box: box.a == 3).update(a=2**40, b=2**40)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_bulk_create_that_overflows_a_check_is_a_data_error(db_url):
    """`bulk_create` on a table with a computing check and no generated
    column reports the overflow as the check's, and inserts no row."""
    Box = declare_box(lambda box: box.a * box.b > 0)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        with overflow_error():
            await Box.bulk_create([Box(a=3, b=4), Box(a=2**40, b=2**40)])
        assert await Box.all() == []


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_false_body_stays_a_check_violation(db_url):
    """The step guard only raises on an overflow; a row the body rejects is
    the check's own violation on both databases."""
    Box = declare_box(lambda box: box.a * box.b > 0)
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        with pytest.raises(CheckViolationError, match="ck_box_c"):
            await Box.create(a=-3, b=4)


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
        (
            "sqlite",
            "CASE WHEN NOT (typeof(abs(\"amount\")) IN ('integer', 'null')) "
            'THEN abs(-9223372036854775807 - 1) ELSE (abs("amount") > 1000) END',
        ),
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


def migrated(project, pkg, db, capsys, models: str = "") -> None:
    """``0001_create_ledger`` applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, models or LEDGER)
    new("create_ledger")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()


def auto_migrated(project, pkg, db, models: str = "") -> None:
    """The ledger built by ``connect(auto_migrate=True)``."""
    write_models(project, pkg, models or LEDGER)
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
        ) == [("CHECK (((sign((amount_minor)::double precision))::bigint <> 0))",)], (
            "the pin is only meaningful if Postgres reprints the body"
        )

    report = drift_api(db)
    assert report.lines == [] and report.clean, report.lines


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_reconnect_runs_nothing_for_a_check_calling_fn(project, pkg, db):
    auto_migrated(project, pkg, db)
    assert reconnect_statements(db) == []
    assert reconnect_statements(db) == []


SUMMED = (
    HEAD
    + """
    __ferro_checks__ = (
        Check("summed", lambda payment: payment.amount_minor + 1 > 0),
    )
"""
)
"""`HEAD` with a check over arithmetic: on SQLite its body carries a step
term, which drift and a reconnect must render the same way."""


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_check_with_step_terms_is_not_drift(project, pkg, db, capsys):
    migrated(project, pkg, db, capsys, SUMMED)
    report = drift_api(db)
    assert report.lines == [] and report.clean, report.lines


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_a_reconnect_runs_nothing_for_a_check_with_step_terms(project, pkg, db):
    auto_migrated(project, pkg, db, SUMMED)
    if db.backend == "sqlite":
        [(create,)] = db.rows(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'payment'"
        )
        assert "typeof" in create, "the pin is only meaningful with a step term"
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
        'CHECK ((CAST(sign("amount_minor") AS bigint) <> 0))',
    ]
    assert reconnect_statements(db) == []
