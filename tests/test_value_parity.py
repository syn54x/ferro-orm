"""Value parity: every `ferro.fn` signature stores the same value on both databases.

Each case declares a model with one generated column, saves the same rows on
SQLite and on Postgres, and asserts both stored what the case expects:

```python
class Sample(Model):
    id: int | None = Field(default=None, primary_key=True)
    a: int
    value: int = Field(generated=lambda sample: fn.least(sample.a, 28))
# a = 31 -> value 28 on SQLite (min) and on Postgres (least)
```

The cases are checked against `_core._value_function_table()`: a signature
the table gains without a case here fails `test_every_signature_has_a_case`.
A failure is part of parity too, so an integer overflow raises `DataError`
naming the column on both.
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar

import pytest

import ferro
from ferro import Check, CheckViolationError, Field, Model, _core, fn
from ferro.exceptions import DataError

pytestmark = [pytest.mark.usefixtures("clean_registry"), pytest.mark.postgres_only]

UTC = datetime.UTC


@dataclass(frozen=True)
class Case:
    """One signature exercised: the model, the rows saved and the values expected."""

    function: str
    args: tuple[str, ...]
    declare: Callable[[], type[Model]]
    rows: tuple[dict[str, Any], ...]
    expected: tuple[Any, ...]


def _numeric_unary(
    name: str, family: str, annotation: Any, inputs, outputs
) -> list[Case]:
    function = getattr(fn, name)

    def declare() -> type[Model]:
        class Sample(Model):
            id: int | None = Field(default=None, primary_key=True)
            a: annotation | None = None
            value: annotation | None = Field(
                generated=lambda sample: function(sample.a)
            )

        return Sample

    return [
        Case(
            name,
            (family,),
            declare,
            tuple({"a": value} for value in (*inputs, None)),
            (*outputs, None),
        )
    ]


def _ordered(name: str, family: str, annotation: Any, rows, outputs) -> Case:
    function = getattr(fn, name)

    def declare() -> type[Model]:
        class Sample(Model):
            id: int | None = Field(default=None, primary_key=True)
            a: annotation
            b: annotation
            value: annotation = Field(
                generated=lambda sample: function(sample.a, sample.b)
            )

        return Sample

    return Case(name, (family, family), declare, tuple(rows), tuple(outputs))


def _coalesce(family: str, annotation: Any, rows, outputs) -> Case:
    def declare() -> type[Model]:
        class Sample(Model):
            id: int | None = Field(default=None, primary_key=True)
            a: annotation | None = None
            b: annotation
            value: annotation = Field(
                generated=lambda sample: fn.coalesce(sample.a, sample.b)
            )

        return Sample

    return Case("coalesce", (family, family), declare, tuple(rows), tuple(outputs))


def _date_part(name: str, outputs) -> Case:
    function = getattr(fn, name)

    def declare() -> type[Model]:
        class Sample(Model):
            id: int | None = Field(default=None, primary_key=True)
            a: datetime.date | None = None
            value: int | None = Field(generated=lambda sample: function(sample.a))

        return Sample

    return Case(name, ("date",), declare, tuple({"a": d} for d in DATES), outputs)


def _length() -> Case:
    def declare() -> type[Model]:
        class Sample(Model):
            id: int | None = Field(default=None, primary_key=True)
            a: str | None = None
            value: int | None = Field(generated=lambda sample: fn.length(sample.a))

        return Sample

    texts = ("", "abc", "naïve 名前 🙂", None)
    return Case(
        "length",
        ("string",),
        declare,
        tuple({"a": text} for text in texts),
        (0, 3, 10, None),
    )


DATES = (
    datetime.date(2024, 2, 29),
    datetime.date(2026, 12, 31),
    datetime.date(1, 1, 1),
    None,
)

D = Decimal
TS = (
    datetime.datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC),
    datetime.datetime(2026, 3, 1, 0, 0, tzinfo=UTC),
)


def _pairs(a, b) -> list[dict[str, Any]]:
    return [{"a": x, "b": y} for x, y in zip(a, b, strict=True)]


CASES: list[Case] = [
    *_numeric_unary("sign", "integer", int, (-5, 0, 7), (-1, 0, 1)),
    *_numeric_unary("sign", "number", float, (-2.5, 0.0, 3.25), (-1.0, 0.0, 1.0)),
    *_numeric_unary(
        "sign", "decimal", Decimal, (D("-1.50"), D("0"), D("2.25")), (D(-1), D(0), D(1))
    ),
    *_numeric_unary("abs", "integer", int, (-5, 0, 7), (5, 0, 7)),
    *_numeric_unary("abs", "number", float, (-2.5, 0.0, 3.25), (2.5, 0.0, 3.25)),
    *_numeric_unary(
        "abs",
        "decimal",
        Decimal,
        (D("-1.50"), D("0"), D("2.25")),
        (D("1.5"), D(0), D("2.25")),
    ),
    _ordered("least", "integer", int, _pairs((5, 31, -1), (3, 40, -1)), (3, 31, -1)),
    _ordered(
        "least", "number", float, _pairs((0.5, -1.25), (0.25, 3.0)), (0.25, -1.25)
    ),
    _ordered(
        "least",
        "decimal",
        Decimal,
        _pairs((D("1.25"), D("-3")), (D("1.5"), D("2"))),
        (D("1.25"), D(-3)),
    ),
    _ordered(
        "least",
        "date",
        datetime.date,
        _pairs(
            DATES[:3],
            (
                datetime.date(2024, 3, 1),
                datetime.date(2026, 11, 30),
                datetime.date(1, 1, 2),
            ),
        ),
        (
            datetime.date(2024, 2, 29),
            datetime.date(2026, 11, 30),
            datetime.date(1, 1, 1),
        ),
    ),
    _ordered("greatest", "integer", int, _pairs((5, 31, -1), (3, 40, -1)), (5, 40, -1)),
    _ordered(
        "greatest", "number", float, _pairs((0.5, -1.25), (0.25, 3.0)), (0.5, 3.0)
    ),
    _ordered(
        "greatest",
        "decimal",
        Decimal,
        _pairs((D("1.25"), D("-3")), (D("1.5"), D("2"))),
        (D("1.5"), D(2)),
    ),
    _ordered(
        "greatest",
        "date",
        datetime.date,
        _pairs(
            DATES[:3],
            (
                datetime.date(2024, 3, 1),
                datetime.date(2026, 11, 30),
                datetime.date(1, 1, 2),
            ),
        ),
        (
            datetime.date(2024, 3, 1),
            datetime.date(2026, 12, 31),
            datetime.date(1, 1, 2),
        ),
    ),
    _coalesce("integer", int, _pairs((None, 5, -3), (0, 9, 9)), (0, 5, -3)),
    _coalesce("number", float, _pairs((None, -0.5), (1.5, 2.0)), (1.5, -0.5)),
    _coalesce(
        "decimal",
        Decimal,
        _pairs((None, D("-0.5")), (D("1.5"), D(2))),
        (D("1.5"), D("-0.5")),
    ),
    _coalesce(
        "string",
        str,
        _pairs((None, "", "名前"), ("anon", "anon", "anon")),
        ("anon", "", "名前"),
    ),
    _coalesce(
        "date",
        datetime.date,
        _pairs((None, DATES[0]), DATES[1:3]),
        (DATES[1], DATES[0]),
    ),
    _coalesce(
        "datetime",
        datetime.datetime,
        _pairs((None, TS[0]), (TS[1], TS[1])),
        (TS[1], TS[0]),
    ),
    _length(),
    _date_part("year", (2024, 2026, 1, None)),
    _date_part("month", (2, 12, 1, None)),
    _date_part("day", (29, 31, 1, None)),
]


def _signature_keys() -> set[tuple[str, tuple[str, ...]]]:
    return {
        (entry["name"], tuple(signature["args"]))
        for entry in json.loads(_core._value_function_table())
        for signature in entry["signatures"]
    }


def test_every_signature_has_a_case():
    covered = {(case.function, case.args) for case in CASES}
    assert _signature_keys() - covered == set()
    assert covered - _signature_keys() == set()


async def _stored(url: str, model: type[Model], rows) -> list[Any]:
    await ferro.connect(url, auto_migrate=True)
    try:
        async with ferro.engines.session():
            stored = []
            for row in rows:
                saved = model(**row)
                await saved.save()
                fetched = await model.get(saved.id)
                assert saved.value == fetched.value
                stored.append(fetched.value)
            return stored
    finally:
        ferro.reset_engine()


def _id(case: Case) -> str:
    return f"{case.function}({', '.join(case.args)})"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=_id)
async def test_each_signature_stores_the_same_value_on_both_databases(
    case: Case, db_url: str, tmp_path
):
    model = case.declare()
    sqlite = await _stored(
        f"sqlite:{tmp_path / 'parity.db'}?mode=rwc", model, case.rows
    )
    postgres = await _stored(db_url, model, case.rows)
    assert sqlite == list(case.expected)
    assert postgres == list(case.expected)


def _declare_arithmetic() -> type[Model]:
    class Sample(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        x: float
        whole: int = Field(generated=lambda sample: (sample.a - 1) * 3 / 2)
        part: float = Field(generated=lambda sample: -sample.x + sample.a)
        day_class: int = Field(generated=lambda sample: fn.least(fn.abs(sample.a), 28))

    return Sample


@pytest.mark.asyncio
async def test_arithmetic_truncates_and_rounds_alike(db_url: str, tmp_path):
    model = _declare_arithmetic()
    rows = [{"a": -6, "x": 0.1}, {"a": 31, "x": -2.5}]

    async def stored(url: str) -> list[tuple[int, float, int]]:
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                saved = [model(**row) for row in rows]
                for instance in saved:
                    await instance.save()
                return [(s.whole, s.part, s.day_class) for s in saved]
        finally:
            ferro.reset_engine()

    sqlite = await stored(f"sqlite:{tmp_path / 'arith.db'}?mode=rwc")
    postgres = await stored(db_url)
    # -21 / 2 truncates toward zero on both databases.
    assert sqlite == postgres == [(-10, -6.1, 6), (45, 33.5, 28)]


def _sum_model(storage: str | None) -> type[Model]:
    options = {"db_type": storage} if storage else {}

    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int = Field(**options)
        b: int = Field(**options)
        total: int = Field(generated=lambda ledger: ledger.a + ledger.b, **options)

    return Ledger


def _abs_model() -> type[Model]:
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int = Field(db_type="bigint")
        total: int = Field(generated=lambda ledger: fn.abs(ledger.a), db_type="bigint")

    return Ledger


def _doubled_model() -> type[Model]:
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: float
        total: float = Field(generated=lambda ledger: ledger.a + ledger.a)

    return Ledger


def _wrapped_step_model(body: str) -> type[Model]:
    """A function over an arithmetic step that can overflow and come back."""
    if body == "greatest":

        class Ledger(Model):
            id: int | None = Field(default=None, primary_key=True)
            x: float
            total: float = Field(
                generated=lambda ledger: fn.greatest(ledger.x + ledger.x, 0.0)
            )

        return Ledger
    bodies = {
        "least": lambda ledger: fn.least(ledger.a * ledger.b, 28),
        "sign": lambda ledger: fn.sign(ledger.a * ledger.b),
    }

    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int = Field(db_type="bigint")
        b: int = Field(db_type="bigint")
        total: int = Field(generated=bodies[body], db_type="bigint")

    return Ledger


#: (declare, the row that overflows, the largest row that fits, its value).
OVERFLOWS = {
    # Postgres raises at `a * b`; SQLite's `min(<real>, 28)` would be 28.
    "fn.least(a * b, 28)": (
        lambda: _wrapped_step_model("least"),
        {"a": 2**62, "b": 4},
        {"a": 2**61, "b": 3},
        28,
    ),
    # SQLite's sign() of the overflowed real would be the integer 1.
    "fn.sign(a * b)": (
        lambda: _wrapped_step_model("sign"),
        {"a": 2**62, "b": 4},
        {"a": 2**61, "b": -3},
        -1,
    ),
    # SQLite's max(inf, 0.0) is inf, and the step's own check fails it.
    "fn.greatest(x + x, 0.0)": (
        lambda: _wrapped_step_model("greatest"),
        {"x": 1e308},
        {"x": 8e307},
        1.6e308,
    ),
    "int at 2**31": (
        lambda: _sum_model(None),
        {"a": 2**31 - 1, "b": 1},
        {"a": 2**31 - 2, "b": 1},
        2**31 - 1,
    ),
    "smallint at 2**15": (
        lambda: _sum_model("smallint"),
        {"a": 2**15 - 1, "b": 1},
        {"a": -(2**15) + 1, "b": -1},
        -(2**15),
    ),
    "bigint at 2**63": (
        lambda: _sum_model("bigint"),
        {"a": 2**63 - 1, "b": 1},
        {"a": 2**62, "b": 2**62 - 1},
        2**63 - 1,
    ),
    # The smallest bigint has no bigint absolute value.
    "abs of the smallest bigint": (
        _abs_model,
        {"a": -(2**63)},
        {"a": -(2**63) + 1},
        2**63 - 1,
    ),
    "float past the largest double": (
        _doubled_model,
        {"a": 1e308},
        {"a": 8e307},
        1.6e308,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(OVERFLOWS))
async def test_an_overflowing_generated_value_raises_data_error_naming_it(
    name: str, db_url: str, tmp_path
):
    declare, overflowing, fitting, value = OVERFLOWS[name]
    Ledger = declare()
    for url in (f"sqlite:{tmp_path / 'overflow.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await Ledger(**overflowing).save()
                fine = Ledger(**fitting)
                await fine.save()
                assert fine.total == pytest.approx(value, rel=1e-12)
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_a_nullable_generated_integer_admits_null(db_url: str, tmp_path):
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int | None = None
        total: int | None = Field(generated=lambda ledger: ledger.a + 1)

    for url in (f"sqlite:{tmp_path / 'null.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                empty = Ledger()
                await empty.save()
                assert empty.total is None
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_a_query_update_that_overflows_names_the_generated_column(
    db_url: str, tmp_path
):
    Ledger = _sum_model(None)
    for url in (f"sqlite:{tmp_path / 'update.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                await Ledger(a=1, b=1).save()
                query = Ledger.where(lambda ledger: ledger.b == 1)
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await query.update(a=2**31 - 1)
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await query.update(lambda ledger: {"a": 2**31 - 1})
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_a_query_update_whose_set_computes_names_both_candidates(
    db_url: str, tmp_path
):
    Ledger = _sum_model(None)
    for url in (f"sqlite:{tmp_path / 'set.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                await Ledger(a=1, b=2**31 - 2).save()
                query = Ledger.where(lambda ledger: ledger.a == 1)
                # SQLite's guard names the column; Postgres cannot tell the
                # SET's own `+` from the generated sum, so it names both.
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await query.update(lambda ledger: {"a": ledger.a + 1})
        finally:
            ferro.reset_engine()


def _middle_step_model(body: str) -> type[Model]:
    bodies = {
        "a + b - c": lambda ledger: ledger.a + ledger.b - ledger.c,
        "fn.abs(a) - 1": lambda ledger: fn.abs(ledger.a) - 1,
        "a * b as bigint": lambda ledger: ledger.a * ledger.b,
    }
    options = {"db_type": "bigint"} if body == "a * b as bigint" else {}

    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int = 0
        c: int = 0
        total: int = Field(generated=bodies[body], **options)

    return Ledger


#: Expressions that leave the inputs' 4-byte range at a middle step: both
#: databases compute them at 64 bits and store the same value.
MIDDLE_STEPS = {
    "a + b - c": ({"a": 2**31 - 1, "b": 1, "c": 1}, 2**31 - 1),
    "fn.abs(a) - 1": ({"a": -(2**31)}, 2**31 - 1),
    "a * b as bigint": ({"a": 2**31 - 1, "b": 2**31 - 1}, (2**31 - 1) ** 2),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", list(MIDDLE_STEPS))
async def test_a_middle_step_past_the_inputs_range_stores_alike(
    body: str, db_url: str, tmp_path
):
    Ledger = _middle_step_model(body)
    row, value = MIDDLE_STEPS[body]
    stored = []
    for url in (f"sqlite:{tmp_path / 'middle.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                saved = Ledger(**row)
                await saved.save()
                stored.append((await Ledger.get(saved.id)).total)
        finally:
            ferro.reset_engine()
    assert stored == [value, value]


@pytest.mark.asyncio
async def test_a_narrower_generated_column_over_wider_inputs_names_it(
    db_url: str, tmp_path
):
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int
        total: int = Field(
            generated=lambda ledger: ledger.a + ledger.b, db_type="smallint"
        )

    for url in (f"sqlite:{tmp_path / 'narrow.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await Ledger(a=2**15, b=0).save()
                fine = Ledger(a=2**15, b=-1)
                await fine.save()
                assert fine.total == 2**15 - 1
        finally:
            ferro.reset_engine()


# -- literals and function results compute at the type SQLite computes them at ------


async def _stored_values(url: str, model: type[Model], rows) -> list[Any]:
    await ferro.connect(url, auto_migrate=True)
    try:
        async with ferro.engines.session():
            values = []
            for row in rows:
                saved = model(**row)
                await saved.save()
                values.append((await model.get(saved.id)).value)
            return values
    finally:
        ferro.reset_engine()


@pytest.mark.asyncio
async def test_a_decimal_literal_computes_in_double_on_both_databases(
    db_url: str, tmp_path
):
    # Postgres types a bare 0.1 as numeric, which would store exactly 0.3.
    class Sample(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        value: float = Field(generated=lambda sample: sample.a + 0.1 + 0.2)

    rows = [{"a": 0}, {"a": 2}]
    sqlite = await _stored_values(
        f"sqlite:{tmp_path / 'literal.db'}?mode=rwc", Sample, rows
    )
    postgres = await _stored_values(db_url, Sample, rows)
    assert sqlite == postgres == [0 + 0.1 + 0.2, 2 + 0.1 + 0.2]


@pytest.mark.asyncio
async def test_a_cast_float_literal_is_no_drift_on_a_reconnect(db_url: str, tmp_path):
    # Postgres prints the CAST back its own way; the pass must still plan nothing.
    class Sample(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        value: float = Field(generated=lambda sample: sample.a + 0.1)

    for url in (f"sqlite:{tmp_path / 'reconnect.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            report = await ferro.migrate(destructive=True)
            assert report.statements == ()
            assert report.warnings == ()
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_the_sign_of_an_integer_divides_as_an_integer_on_both_databases(
    db_url: str, tmp_path
):
    # Postgres's sign(bigint) is a double: 3 / 2 would be 1.5, stored as 2.
    class Sample(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: int
        value: int = Field(generated=lambda sample: fn.sign(sample.a) * 3 / 2)

    rows = [{"a": 7}, {"a": -7}, {"a": 0}]
    sqlite = await _stored_values(
        f"sqlite:{tmp_path / 'sign.db'}?mode=rwc", Sample, rows
    )
    postgres = await _stored_values(db_url, Sample, rows)
    assert sqlite == postgres == [1, -1, 0]


@pytest.mark.asyncio
async def test_a_plain_column_overflow_stays_an_operational_error(db_url: str):
    # Only a generated column's overflow is re-classed as DataError.
    class Counter(Model):
        id: int | None = Field(default=None, primary_key=True)
        hits: int = Field(db_type="smallint")

    await ferro.connect(db_url, auto_migrate=True)
    try:
        async with ferro.engines.session():
            with pytest.raises(ferro.OperationalError) as raised:
                await Counter(hits=2**15).save()
            assert not isinstance(raised.value, DataError)
            assert raised.value.sqlstate == "22003"
    finally:
        ferro.reset_engine()


# -- a step Postgres never evaluates fails on neither database ----------------------


@pytest.mark.asyncio
async def test_a_coalesce_fallback_postgres_skips_stores_alike(db_url: str, tmp_path):
    # Postgres stops at the first non-null argument, so `a + 1` only
    # overflows when `n` is NULL; the SQLite guard must not compute it sooner.
    class Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int | None = Field(default=None, db_type="bigint")
        a: int = Field(db_type="bigint")
        total: int = Field(
            generated=lambda ledger: fn.coalesce(ledger.n, ledger.a + 1),
            db_type="bigint",
        )

    for url in (f"sqlite:{tmp_path / 'coalesce.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                kept = Ledger(n=5, a=2**63 - 1)
                await kept.save()
                assert (await Ledger.get(kept.id)).total == 5
                with pytest.raises(DataError, match=r'generated column "total"'):
                    await Ledger(a=2**63 - 1).save()
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_a_coalesce_fallback_in_a_check_postgres_skips_passes_alike(
    db_url: str, tmp_path
):
    class Box(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("c", lambda box: fn.coalesce(box.n, box.b + 1) > 0),
        )
        id: int | None = Field(default=None, primary_key=True)
        n: int | None = Field(default=None, db_type="bigint")
        b: int = Field(db_type="bigint")

    for url in (f"sqlite:{tmp_path / 'coalesce_check.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                await Box.create(n=5, b=2**63 - 1)
                with pytest.raises(DataError, match='the check "ck_box_c" of "box"'):
                    await Box.create(b=2**63 - 1)
                assert len(await Box.all()) == 1
        finally:
            ferro.reset_engine()


@pytest.mark.asyncio
async def test_an_integer_beside_a_float_compares_as_a_double_on_both_databases(
    db_url: str, tmp_path
):
    # Postgres converts `a` to a double for greatest(bigint, double), so
    # 2**53 + 1 rounds to 2**53; SQLite's max() would keep the integer exact.
    class Box(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("c", lambda box: fn.greatest(box.a, 0.5) != 9007199254740992.0),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int = Field(db_type="bigint")

    for url in (f"sqlite:{tmp_path / 'greatest.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                await Box.create(a=3)
                with pytest.raises(CheckViolationError, match="ck_box_c"):
                    await Box.create(a=2**53 + 1)
        finally:
            ferro.reset_engine()


# A check comparing a bigint with a float through a value expression
# decides alike on both databases: Postgres compares a value expression's
# float at double, so 2**53 + 1 is 2**53 there, and SQLite casts the integer
# to match. A plain comparison is outside value parity and renders as it
# always has; its bare float literal is an exact numeric on Postgres and
# SQLite compares it exactly too, so those cases agree anyway.
TWO_53 = 9007199254740992.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (lambda box: box.a != 9007199254740992.0, True),
        (lambda box: box.a == 9007199254740992.0, False),
        (lambda box: box.a > 0.5, True),
        (lambda box: box.a + 0 != 9007199254740992.0, False),
        (lambda box: 9007199254740992.0 != box.a + 0, False),
        (lambda box: box.a + 0 != box.x, False),
    ],
    ids=[
        "a != 2**53.0",
        "a == 2**53.0",
        "a > 0.5",
        "a + 0 != 2**53.0",
        "2**53.0 != a + 0",
        "a + 0 != x",
    ],
)
async def test_an_integer_compared_with_a_float_decides_alike_on_both(
    body, accepted: bool, db_url: str, tmp_path
):
    class Box(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (Check("c", body),)
        id: int | None = Field(default=None, primary_key=True)
        a: int = Field(db_type="bigint")
        x: float = TWO_53

    outcomes = []
    for url in (f"sqlite:{tmp_path / 'compare.db'}?mode=rwc", db_url):
        await ferro.connect(url, auto_migrate=True)
        try:
            async with ferro.engines.session():
                try:
                    await Box.create(a=2**53 + 1)
                    outcomes.append(True)
                except CheckViolationError:
                    outcomes.append(False)
            # The cast reads back as declared: a reconnect plans nothing.
            report = await ferro.migrate(destructive=True)
            assert report.statements == ()
            assert report.warnings == ()
        finally:
            ferro.reset_engine()
    assert outcomes == [accepted, accepted]


def _int_column(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "logical_type": "integer",
        "db_type": None,
        "db_type_explicit": False,
        "nullable": False,
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
    }


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_an_integer_comparison_renders_as_before(dialect: str):
    # `price > 0` is integers on both sides, so it carries no cast.
    predicate = {
        "kind": "cmp",
        "column": "price",
        "op": "gt",
        "other": {"kind": "literal", "token": "0"},
    }
    rendered = _core._render_table_check_body(
        json.dumps(predicate), dialect, json.dumps([_int_column("price")])
    )
    assert rendered == '"price" > 0'


@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
def test_a_plain_integer_float_column_comparison_renders_as_before(dialect: str):
    # `qty < price`, an `int` and a `float` column: a plain comparison, outside
    # value parity, renders as it did before value expressions, with no
    # `CAST(... AS real)`, so a database a released Ferro wrote reads clean.
    predicate = {
        "kind": "cmp",
        "column": "qty",
        "op": "lt",
        "other": {"kind": "column", "name": "price"},
    }
    price = {**_int_column("price"), "logical_type": "number"}
    rendered = _core._render_table_check_body(
        json.dumps(predicate), dialect, json.dumps([_int_column("qty"), price])
    )
    assert rendered == '"qty" < "price"'
