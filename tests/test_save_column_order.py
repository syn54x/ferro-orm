"""A write lists its columns in the model's field order (#776).

```python
class Customer(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    updated_at: datetime | None = None

await customer.save(only={"updated_at", "name"})
# UPDATE "customer" SET "name" = ?, "updated_at" = ? WHERE "id" = ?
```

``only=`` and ``exclude=`` take a set, whose iteration order changes with
the process's hash seed, and a fetched instance holds its values in the
order the ``SELECT`` listed them. Neither may reach the statement: every
write lists its columns in the order the model declares its fields, so the
statement log and ``.sql()`` show one statement for one call. A bytes or
secret column, bound raw rather than through the JSON dump, keeps its
declared place too.
"""

import logging
import re
from typing import Annotated

import pytest
import pytest_asyncio
from pydantic import SecretStr

import ferro
from ferro import Model, engines
from ferro.base import FerroField

pytestmark = pytest.mark.backend_matrix

# Declared out of alphabetical order, which is the order a fetch hydrates in.
FIELDS = ["h", "b", "blob", "f", "a", "token", "d", "c", "g", "e"]


class ColOrder(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    h: str = "h"
    b: str = "b"
    blob: bytes = b"\x00"
    f: int = 0
    a: str = "a"
    token: SecretStr = SecretStr("s")
    d: int = 0
    c: str = "c"
    g: str = "g"
    e: str = "e"


class PlainOrder(Model):
    """No bytes or secret column: every value goes through the JSON dump."""

    id: Annotated[int | None, FerroField(primary_key=True)] = None
    zeta: str = "z"
    mid: int = 0
    alpha: str = "a"


SET_COLUMN = re.compile(r'"(\w+)" = ')


def _set_columns(sql: str) -> list[str]:
    """The columns of an ``UPDATE``'s ``SET`` clause, in the order it lists them."""
    head, _, _ = sql.partition(" WHERE ")
    _, _, assignments = head.partition(" SET ")
    return SET_COLUMN.findall(assignments)


def _insert_columns(sql: str) -> list[str]:
    columns = sql.partition("(")[2].partition(")")[0]
    return [c for c in re.findall(r'"(\w+)"', columns) if c != "id"]


def _sent(records: list[logging.LogRecord], prefix: str) -> list[str]:
    return [
        r.sql
        for r in records
        if r.operation not in ("migrate", "catalog") and r.sql.startswith(prefix)
    ]


def _in_field_order(columns: set[str]) -> list[str]:
    return [name for name in FIELDS if name in columns]


@pytest_asyncio.fixture
async def saved_row(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with engines.session():
        await ColOrder(id=1).save()
        await PlainOrder(id=1).save()
    yield
    ferro.reset_engine()


ONLY = {"h", "b", "token", "f", "a", "blob", "d"}
EXCLUDE = {"c", "g"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_save_only_lists_columns_in_field_order(statement_records):
    async with engines.session():
        row = await ColOrder.get(1)
        await row.save(only=ONLY)

    (update,) = _sent(statement_records, "UPDATE")
    assert _set_columns(update) == _in_field_order(ONLY)


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_save_only_two_columns_in_field_order(statement_records):
    async with engines.session():
        row = await ColOrder.get(1)
        await row.save(only={"a", "b"})

    (update,) = _sent(statement_records, "UPDATE")
    assert _set_columns(update) == ["b", "a"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_save_exclude_lists_columns_in_field_order(statement_records):
    async with engines.session():
        row = await ColOrder.get(1)
        await row.save(exclude=EXCLUDE)

    (update,) = _sent(statement_records, "UPDATE")
    assert _set_columns(update) == [n for n in FIELDS if n not in EXCLUDE]


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_a_full_save_lists_columns_in_field_order(statement_records):
    async with engines.session():
        row = await ColOrder.get(1)
        await row.save()
        await ColOrder(id=2).save()

    (update,) = _sent(statement_records, "UPDATE")
    assert _set_columns(update) == FIELDS
    (insert,) = _sent(statement_records, "INSERT")
    assert _insert_columns(insert) == FIELDS


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_a_fetched_instance_without_raw_columns_writes_in_field_order(
    statement_records,
):
    async with engines.session():
        row = await PlainOrder.get(1)
        await row.save()
        await row.save(only={"alpha", "zeta"})
        await row.save(exclude={"mid"})

    updates = _sent(statement_records, "UPDATE")
    assert [_set_columns(update) for update in updates] == [
        ["zeta", "mid", "alpha"],
        ["zeta", "alpha"],
        ["zeta", "alpha"],
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_bulk_create_lists_columns_in_field_order(statement_records):
    async with engines.session():
        await ColOrder.bulk_create([ColOrder(id=3), ColOrder(id=4)])

    (insert,) = _sent(statement_records, "INSERT")
    assert _insert_columns(insert) == FIELDS


@pytest.mark.asyncio
@pytest.mark.usefixtures("saved_row")
async def test_query_update_lists_columns_in_keyword_order(statement_records):
    async with engines.session():
        await ColOrder.where(lambda col_order: col_order.id == 1).update(
            e="x", a="y", d=3
        )

    (update,) = _sent(statement_records, "UPDATE")
    assert _set_columns(update) == ["e", "a", "d"]
