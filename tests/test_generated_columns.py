"""Generated columns: a field whose value the database computes (ADR-0058).

```python
class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount_minor: int
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.amount_minor), index=True
    )
```

```sql
"direction" integer NOT NULL GENERATED ALWAYS AS (sign("amount_minor")) STORED
```

The instance holds the value the database returned, or ``None`` while it is
unknown: before the first save, and after a column the expression reads is
assigned. ``save()`` reads it back with ``RETURNING`` on both dialects, and no
statement Ferro writes ever lists the column.
"""

# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
from __future__ import annotations

import asyncio
import json
from typing import Annotated

import pytest

import ferro
from ferro import Check, Field, Model, _core, fn
from ferro.base import FerroField
from ferro.ir import compile_registry_schema_ir
import ferro.migrations
from tests._pass_harness import auto_migrate, schema_sql
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


def generated_sql(backend: str) -> str:
    """The clause per dialect: Postgres computes an `integer` input at 64 bits."""
    operand = (
        'CAST("amount_minor" AS bigint)' if backend == "postgres" else '"amount_minor"'
    )
    return f"GENERATED ALWAYS AS (sign({operand})) STORED"


def declare_transaction() -> type[Model]:
    class Transaction(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        direction: int = Field(
            generated=lambda transaction: fn.sign(transaction.amount_minor),
            index=True,
        )

    return Transaction


def declare_annotated_transaction() -> type[Model]:
    class Transaction(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount_minor: int
        direction: Annotated[
            int,
            Field(
                generated=lambda transaction: fn.sign(transaction.amount_minor),
                index=True,
            ),
        ]

    return Transaction


STYLES = pytest.mark.parametrize(
    "declare",
    [declare_transaction, declare_annotated_transaction],
    ids=["assignment", "annotated"],
)


def _column(table: str, name: str) -> dict:
    envelope = compile_registry_schema_ir()
    model = next(m for m in envelope["payload"]["models"] if m["table_name"] == table)
    return next(c for c in model["columns"] if c["name"] == name)


def _save_sql(records) -> list[str]:
    return [r.sql for r in records if r.operation == "save"]


# -- the declaration ------------------------------------------------------------------


@STYLES
def test_the_schema_carries_the_lowered_expression_and_the_field_defaults_to_none(
    declare,
):
    Transaction = declare()

    assert _column("transaction", "direction")["generated"] == {
        "kind": "func",
        "name": "sign",
        "args": [{"kind": "column", "name": "amount_minor"}],
    }
    assert "generated" not in _column("transaction", "amount_minor")
    assert _column("transaction", "direction")["nullable"] is False
    assert Transaction.__ferro_generated__ == {"direction": frozenset({"amount_minor"})}
    assert Transaction(amount_minor=-5).direction is None


def test_arithmetic_and_unary_minus_lower_to_binary_and_negate():
    class Line(Model):
        id: int | None = Field(default=None, primary_key=True)
        price: int
        qty: int
        total: int = Field(generated=lambda line: -(line.price * line.qty) + 1)
        half: int = Field(generated=lambda line: line.price / 2 - line.qty)

    column = lambda name: {"kind": "column", "name": name}  # noqa: E731
    assert _column("line", "total")["generated"] == {
        "kind": "binary",
        "op": "add",
        "left": {
            "kind": "negate",
            "operand": {
                "kind": "binary",
                "op": "mul",
                "left": column("price"),
                "right": column("qty"),
            },
        },
        "right": {"kind": "literal", "token": "1"},
    }
    assert _column("line", "half")["generated"] == {
        "kind": "binary",
        "op": "sub",
        "left": {
            "kind": "binary",
            "op": "div",
            "left": column("price"),
            "right": {"kind": "literal", "token": "2"},
        },
        "right": column("qty"),
    }
    assert Line.__ferro_generated__["total"] == frozenset({"price", "qty"})


def test_a_misspelled_column_is_refused_with_a_suggestion():
    with pytest.raises(TypeError, match="Did you mean 'amount_minor'"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            direction: int = Field(
                generated=lambda transaction: fn.sign(transaction.amount_minr)
            )


def test_a_closure_is_refused():
    offset = 3
    with pytest.raises(TypeError, match="closes over 'offset'"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            shifted: int = Field(
                generated=lambda transaction: transaction.amount_minor + offset
            )


def test_a_python_call_is_refused_naming_fn():
    with pytest.raises(TypeError, match="ferro.fn"):

        class Transaction(Model):
            id: int | None = Field(default=None, primary_key=True)
            amount_minor: int
            magnitude: int = Field(
                generated=lambda transaction: abs(transaction.amount_minor)
            )


def test_an_unknown_function_lists_the_table():
    names = [entry["name"] for entry in json.loads(_core._value_function_table())]
    with pytest.raises(AttributeError, match="nope") as caught:
        fn.nope  # noqa: B018
    assert all(name in str(caught.value) for name in names)


def test_fn_is_the_rust_function_table():
    names = {entry["name"] for entry in json.loads(_core._value_function_table())}
    assert names == set(fn.__all__)
    assert all(callable(getattr(fn, name)) for name in names)


def test_a_check_still_refuses_a_function_call():
    with pytest.raises(TypeError, match="function call"):

        class Payment(Model):
            __ferro_checks__ = (Check("x", lambda bad: bad.amount > abs(5)),)
            id: int | None = Field(default=None, primary_key=True)
            amount: int


# -- the instance ---------------------------------------------------------------------


@STYLES
def test_constructing_a_generated_field_is_refused_naming_the_argument(declare):
    Transaction = declare()
    with pytest.raises(TypeError, match=r"drop direction= from the call"):
        Transaction(amount_minor=-5, direction=1)


def test_assigning_a_generated_field_is_refused_naming_its_inputs():
    Transaction = declare_transaction()
    transaction = Transaction(amount_minor=-5)
    with pytest.raises(TypeError, match="amount_minor"):
        transaction.direction = 1


def test_assigning_an_input_clears_the_generated_field():
    Transaction = declare_transaction()
    transaction = Transaction(amount_minor=-5)
    transaction.__dict__["direction"] = -1
    transaction.amount_minor = 7
    assert transaction.direction is None


@pytest.mark.asyncio
async def test_update_refuses_a_generated_target_as_keyword_and_recipe():
    Transaction = declare_transaction()
    query = Transaction.where(lambda transaction: transaction.id == 1)
    with pytest.raises(TypeError, match="generated column .* amount_minor"):
        await query.update(direction=1)
    with pytest.raises(TypeError, match="generated column .* amount_minor"):
        await query.update(lambda transaction: {"direction": 1})


@pytest.mark.asyncio
async def test_update_refuses_multiplication_and_division():
    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        views: int

    query = Post.where(lambda post: post.id == 1)
    with pytest.raises(TypeError, match=r"not \*"):
        await query.update(lambda post: {"views": post.views * 2})
    with pytest.raises(TypeError, match="not /"):
        await query.update(lambda post: {"views": post.views / 2})


# -- the database ---------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_the_create_pass_writes_the_stored_clause_with_no_default(db_url):
    declare_transaction()
    report = await auto_migrate(db_url)
    create = next(s for s in schema_sql(report) if s.startswith("CREATE TABLE"))
    backend = "postgres" if db_url.startswith("postgres") else "sqlite"
    assert '"direction" integer NOT NULL ' + generated_sql(backend) in create
    assert "DEFAULT" not in create
    assert any("idx_transaction_direction" in s for s in schema_sql(report))


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@STYLES
async def test_insert_update_and_fetch_hold_the_database_value(
    declare, db_url, statement_records
):
    Transaction = declare()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        transaction = Transaction(amount_minor=-5)
        assert transaction.direction is None

        del statement_records[:]
        await transaction.save()
        assert transaction.id == 1
        assert transaction.direction == -1
        assert "direction" not in transaction.model_fields_set
        (insert,) = _save_sql(statement_records)
        assert insert.startswith('INSERT INTO "transaction" ("amount_minor")')
        assert insert.endswith('RETURNING "id", "direction"')

        transaction.amount_minor = 7
        assert transaction.direction is None
        del statement_records[:]
        await transaction.save()
        assert transaction.direction == 1
        assert "direction" not in transaction.model_fields_set
        (update,) = _save_sql(statement_records)
        assert '"direction" =' not in update
        assert update.endswith('RETURNING "direction"')

    async with ferro.engines.session():
        fetched = await Transaction.get(1)
        assert fetched.direction == 1
        assert fetched.model_dump()["direction"] == 1


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_query_filters_on_a_generated_column_and_bulk_create_leaves_none(
    db_url,
):
    Transaction = declare_transaction()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        rows = [Transaction(amount_minor=amount) for amount in (-3, 0, 4)]
        assert await Transaction.bulk_create(rows) == 3
        assert [row.direction for row in rows] == [None, None, None]

        outflows = await Transaction.where(
            lambda transaction: transaction.direction < 0
        ).all()
        assert [row.amount_minor for row in outflows] == [-3]

        with pytest.raises(TypeError, match="generated"):
            await Transaction.where(lambda transaction: transaction.id == 1).update(
                direction=1
            )
        saved = await Transaction.get(1)
        with pytest.raises(ValueError, match="generated"):
            await saved.save(only={"direction"})


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_an_auto_key_with_no_generated_column_reads_back_the_key(
    db_url, statement_records
):
    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        del statement_records[:]
        author = Author(name="Ada")
        await author.save()
        assert author.id == 1
        (insert,) = _save_sql(statement_records)
        assert insert.endswith('RETURNING "id"')

        given = Author(id=7, name="Grace")
        del statement_records[:]
        await given.save()
        (insert,) = _save_sql(statement_records)
        assert "RETURNING" not in insert


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_reconnect_plans_nothing_for_a_fresh_table(db_url):
    declare_transaction()
    await ferro.connect(db_url, auto_migrate=True)
    ferro.reset_engine()

    report = await auto_migrate(db_url, updates=True, destructive=True)
    assert schema_sql(report) == []
    assert list(report.warnings) == []
    ferro.reset_engine()


LEDGER = """
from ferro import Field, fn


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount_minor: int
    direction: int = Field(
        generated=lambda transaction: fn.sign(transaction.amount_minor), index=True
    )
"""


@pytest.mark.backend_matrix
@pytest.mark.usefixtures("isolated_imports")
def test_drift_after_the_migrations_door_creates_the_table_is_empty(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, LEDGER)
    new("create_ledger")
    step = next(migrations(project).glob("0001_create_ledger/01_schema.up.*.sql"))
    assert generated_sql(db.backend) in step.read_text()
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    report = asyncio.run(ferro.migrations.drift(url=db.url))
    assert report.clean, report.lines
    assert report.operations == []


def test_the_json_schema_carries_no_lambda():
    Transaction = declare_transaction()
    schema = json.dumps(Transaction.model_json_schema())
    assert "direction" in schema


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_generated_column_over_the_auto_key_holds_its_value_after_insert(
    db_url,
):
    class Order(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str
        ref: int = Field(generated=lambda order: order.id * 1000)

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        order = Order(label="first")
        await order.save()
        assert (order.id, order.ref) == (1, 1000)


def test_ferro_field_takes_generated_in_annotated_style():
    class Transaction(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount_minor: int
        direction: Annotated[
            int,
            FerroField(generated=lambda transaction: fn.sign(transaction.amount_minor)),
        ] = None  # type: ignore[assignment]

    assert Transaction.__ferro_generated__ == {"direction": frozenset({"amount_minor"})}
    assert _column("transaction", "direction")["generated"]["name"] == "sign"
    assert Transaction(amount_minor=3).direction is None
