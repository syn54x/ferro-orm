"""A join table's key column is stored as the primary key it references.

```python
class Invoice(Model):
    number: Decimal = Field(primary_key=True)
    tags: Relation[list[Tag]] = ManyToMany(through="invoice_tags")
```

`invoice_tags.invoice_id` is `numeric` on Postgres and `NUMERIC` on SQLite,
as `invoice.number` is: each join column is derived from the column spec of
the primary key it points at, the one rule a declared foreign-key column is
checked against. A join column already stored otherwise is any column's
type change to the one planner.
"""

import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import Field, ManyToMany, Model, Relation
from ferro.raw import execute, fetch_all
from ferro.registry import REGISTRY
from ferro.relations import resolve_relationships
from ferro.relations.fixes import DeclarationRefusal
from tests._pass_harness import (
    auto_migrate,
    schema_steps,
    warned_auto_migrate,
    warning_texts,
)


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


# Each key: its annotation and the Field options beside primary_key=True.
_KEYS = {
    "Decimal": (Decimal, {}),
    "date": (datetime.date, {}),
    "datetime": (datetime.datetime, {}),
    "time": (datetime.time, {}),
    "UUID": (UUID, {"default_factory": uuid4}),
    "Enum": (Color, {}),
    "varchar(36)": (str, {"db_type": "varchar(36)"}),
    "int": (int | None, {"default": None}),
    "float": (float, {}),
    "bool": (bool, {}),
    "bytes": (bytes, {}),
}


@pytest.fixture
def fresh():
    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()
    yield
    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


def _models(annotation, options):
    """A ``Tag`` and an ``Owner`` keyed by ``annotation``, linked through ``owner_tags``."""

    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)

    owner = type(
        "Owner",
        (Model,),
        {
            "__module__": __name__,
            "__annotations__": {"key": annotation, "tags": Relation[list[Tag]]},
            "key": Field(primary_key=True, **options),
            "tags": ManyToMany(through="owner_tags"),
        },
    )
    return owner, Tag


async def _column_type(url: str, table: str, column: str) -> tuple:
    """The catalog's type of ``table.column``: SQLite's declared type, or
    Postgres's ``data_type``, length and ``udt_name``."""
    async with ferro.engines.session():
        if url.startswith("sqlite"):
            rows = await fetch_all(
                f"SELECT name, type FROM pragma_table_info('{table}')"
            )
            return tuple(row["type"] for row in rows if row["name"] == column)
        rows = await fetch_all(
            "SELECT data_type, character_maximum_length, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = $1 "
            "AND column_name = $2",
            table,
            column,
        )
    (row,) = rows
    return (row["data_type"], row["character_maximum_length"], row["udt_name"])


def _invoice_models():
    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Invoice(Model):
        number: Decimal = Field(primary_key=True)
        tags: Relation[list[Tag]] = ManyToMany(through="invoice_tags")

    return Invoice, Tag


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_decimal_key_gives_its_join_column_numeric_on_sqlite(db_url, fresh):
    _invoice_models()

    await auto_migrate(db_url)

    assert await _column_type(db_url, "invoice_tags", "invoice_id") == ("NUMERIC",)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_decimal_key_gives_its_join_column_numeric_on_postgres(db_url, fresh):
    _invoice_models()

    report = await auto_migrate(db_url)

    assert report is not None
    (data_type, _, _) = await _column_type(db_url, "invoice_tags", "invoice_id")
    assert data_type == "numeric"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("key", list(_KEYS))
async def test_each_join_column_is_stored_as_its_key(db_url, fresh, key):
    _models(*_KEYS[key])

    await auto_migrate(db_url)

    assert await _column_type(db_url, "owner_tags", "owner_id") == await _column_type(
        db_url, "owner", "key"
    )


def test_a_join_column_is_a_plain_foreign_key_of_its_key_s_type(fresh):
    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        tags: Relation[list[Tag]] = ManyToMany(through="post_tags")

    resolve_relationships()

    columns = {c.name: c for c in REGISTRY.join_tables()["post_tags"].columns}
    post_id = columns["post_id"]
    assert post_id.python_type is int
    assert post_id.foreign_key.to_table == "post"
    assert not any(
        (
            post_id.nullable,
            post_id.primary_key,
            post_id.autoincrement,
            post_id.unique,
            post_id.index,
            post_id.db_check,
        )
    )


def test_a_many_to_many_to_a_keyless_model_is_refused_with_an_int_key(fresh):
    class Tag(Model):
        name: str = ""

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        tags: Relation[list[Tag]] = ManyToMany(through="post_tags")

    with pytest.raises(
        DeclarationRefusal,
        match=r"Tag declares no primary key .* on Tag, add "
        r"`id: int \| None = Field\(default=None, primary_key=True\)`",
    ):
        resolve_relationships()


async def _recreate_join_table(url, table, column, stored, mistyped):
    """Build every table from the models, then ``table`` again as it was
    created before join columns took their key's storage: ``column`` stored
    as ``mistyped`` where the models say ``stored``."""
    report = await auto_migrate(url)
    statements = [sql for subject, sql in schema_steps(report) if subject == table]
    async with ferro.engines.session():
        await execute(f'DROP TABLE "{table}"')
        for sql in statements:
            await execute(sql.replace(f'"{column}" {stored}', f'"{column}" {mistyped}'))
    ferro.reset_engine()


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_varchar_join_column_on_sqlite_is_left_with_a_warning(db_url, fresh):
    _invoice_models()
    await _recreate_join_table(
        db_url, "invoice_tags", "invoice_id", "NUMERIC", "varchar"
    )

    report = await warned_auto_migrate(db_url, destructive=True)

    assert [s for s, _ in schema_steps(report) if s == "invoice_tags"] == []
    assert [w.kind for w in report.warnings] == ["SqliteInPlace"]
    (text,) = warning_texts(report)
    assert "invoice_tags.invoice_id" in text
    assert "'varchar'" in text and "'NUMERIC'" in text


def _code_models():
    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)

    class Code(Model):
        code: str = Field(primary_key=True, db_type="varchar(36)")
        tags: Relation[list[Tag]] = ManyToMany(through="code_tags")

    return Code, Tag


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_varchar_join_column_on_sqlite_under_a_varchar_36_key_is_left_quietly(
    db_url, fresh
):
    _code_models()
    await _recreate_join_table(db_url, "code_tags", "code_id", "varchar(36)", "varchar")

    report = await warned_auto_migrate(db_url, destructive=True)

    assert await _column_type(db_url, "code_tags", "code_id") == ("varchar",)
    assert schema_steps(report) == []
    assert list(report.warnings) == []


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_varchar_join_column_on_postgres_is_altered_to_its_key(db_url, fresh):
    _code_models()
    await _recreate_join_table(db_url, "code_tags", "code_id", "varchar(36)", "varchar")

    report = await auto_migrate(db_url, destructive=True)

    assert (
        'ALTER TABLE "code_tags" ALTER COLUMN "code_id" TYPE varchar(36) '
        'USING "code_id"::varchar(36)'
    ) in [sql for _, sql in schema_steps(report)]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_decimal_keyed_join_table_migrates_to_nothing(db_url, fresh):
    _invoice_models()
    await auto_migrate(db_url)
    ferro.reset_engine()

    report = await auto_migrate(db_url, destructive=True)

    assert schema_steps(report) == []
    assert list(report.warnings) == []
