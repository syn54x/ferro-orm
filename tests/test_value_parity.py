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
from typing import Any

import pytest

import ferro
from ferro import Field, Model, _core, fn
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


#: (declare, the row that overflows, the largest row that fits, its value).
OVERFLOWS = {
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
