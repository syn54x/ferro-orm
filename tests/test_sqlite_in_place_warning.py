# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The warning SQLite gives for a change it can make only by rebuilding a table.

```python
class Note(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    prefs: dict
```

Over a table made by hand with `"prefs" json_text NOT NULL`, the pass runs
nothing and warns:

```text
Column 'note.prefs' has type 'json_text' but the model expects 'JSON', and
SQLite cannot change a column's type in place. To make this change, rebuild
the table by hand (…) and generate this change as the next migration.
```

Every `SqliteInPlace` warning ends in the same fix sentence. The pass runs
only on a database with no migration records, where `ferro migrate new`
alone writes the models as they are now and `ferro migrate baseline` then
refuses the very difference the warning reported, so the sentence names
the two fixes that work there.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import sys
import warnings

import pytest

import ferro
from ferro import _core
from ferro.ir.compiler import compile_registry_schema_ir
from tests._pass_harness import auto_migrate, schema_sql
from tests.test_generate_columns import no_bytecode  # noqa: F401 - fixture
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    run,
    write_config,
    write_models,
)
from tests.test_migrate_up import db  # noqa: F401 - fixture

SQLITE_IN_PLACE_FIX = (
    "To make this change, rebuild the table by hand (create it under a new name "
    "as the models declare it, copy the rows, drop the old table, rename the new "
    "one); or, if the models can be put back to what the database holds, adopt "
    "migrations there (`ferro migrate new`, then `ferro migrate baseline`) and "
    "generate this change as the next migration."
)
"""The fix every SQLite in-place warning ends with, word for word."""

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry", "no_bytecode"),
    pytest.mark.backend_matrix,
]

sqlite_only = pytest.mark.sqlite_only


def declare(project, pkg, body: str) -> None:
    """``body`` as the registered models."""
    write_models(project, pkg, body)
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    sys.modules.pop(f"{pkg}.models", None)
    importlib.import_module(f"{pkg}.models")


def migrate(db) -> ferro.PassReport:
    """``ferro.migrate(destructive=True)`` over ``db``, its warnings kept."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        report = asyncio.run(auto_migrate(db.url, destructive=True))
    ferro.reset_engine()
    return report


def in_place_texts(report: ferro.PassReport) -> list[str]:
    return [w.text for w in report.warnings if w.kind == "SqliteInPlace"]


def cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(out),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore")
        code = run(*argv)
    return code, out.getvalue()


NOTE = """
class R997Note(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    {field}
"""

STATUS = """
class R997Status(StrEnum):
    PAID = "paid"
    CANCELLED = "cancelled"
""" + NOTE.format(field="status: R997Status")


@sqlite_only
def test_a_hand_written_type_name_is_printed_as_the_database_holds_it(project, pkg, db):
    db.execute(
        'CREATE TABLE "r997note" ("id" integer PRIMARY KEY AUTOINCREMENT, '
        '"prefs" json_text NOT NULL)'
    )
    declare(project, pkg, NOTE.format(field="prefs: dict"))

    report = migrate(db)

    assert schema_sql(report) == []
    assert in_place_texts(report) == [
        "Column 'r997note.prefs' has type 'json_text' but the model expects "
        "'JSON', and SQLite cannot change a column's type in place. "
        + SQLITE_IN_PLACE_FIX
    ]


@sqlite_only
@pytest.mark.parametrize(
    ("live", "field", "differs"),
    [
        pytest.param(
            "char(5)",
            "code: int",
            "Column 'r997note.code' has type 'char(5)' but the model expects 'integer'",
            id="char(5) reads as text",
        ),
        pytest.param(
            "boolean",
            "code: str",
            "Column 'r997note.code' has type 'boolean' but the model expects 'varchar'",
            id="boolean reads as int",
        ),
        pytest.param(
            "",
            "code: int",
            "Column 'r997note.code' has no declared type but the model expects "
            "'integer'",
            id="no type name",
        ),
    ],
)
def test_a_live_type_name_its_token_does_not_spell_is_printed_as_held(
    project, pkg, db, live, field, differs
):
    db.execute(
        'CREATE TABLE "r997note" ("id" integer PRIMARY KEY AUTOINCREMENT, '
        f'"code" {live} NOT NULL)'
    )
    declare(project, pkg, NOTE.format(field=field))

    assert in_place_texts(migrate(db)) == [
        f"{differs}, and SQLite cannot change a column's type in place. "
        + SQLITE_IN_PLACE_FIX
    ]


@sqlite_only
def test_a_bounded_column_ferro_created_is_no_change_on_the_next_pass(project, pkg, db):
    """`varchar(8)`, a `max_length` and a UUID's `CHAR(32)` read back as no
    change: no warning, and no drift once migrations adopt it."""
    models = """
    import uuid

    class R997Bound(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        key: uuid.UUID
        tag: Annotated[str, FerroField(db_type="varchar(8)")]
        short: str = Field(max_length=8)
    """
    declare(project, pkg, models)
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()

    report = migrate(db)

    assert schema_sql(report) == []
    assert report.warnings == ()
    write_config(project, pkg, '["sqlite"]')
    for argv in (
        ("migrate", "new", "adopt"),
        ("migrate", "baseline", "--url", db.url),
        ("migrate", "drift", "--url", db.url),
    ):
        code, output = cli(*argv)
        assert code == 0, (argv, output)


@sqlite_only
def test_the_expected_type_is_the_one_the_create_pass_writes(project, pkg, db):
    declare(project, pkg, STATUS)
    created = schema_sql(migrate(db))
    assert '"status" varchar(9) NOT NULL' in created[0], created
    db.execute('DROP TABLE "r997note"')
    db.execute(
        'CREATE TABLE "r997note" ("id" integer PRIMARY KEY AUTOINCREMENT, '
        '"status" integer NOT NULL)'
    )

    report = migrate(db)

    assert schema_sql(report) == []
    assert in_place_texts(report) == [
        "Column 'r997note.status' has type 'integer' but the model expects "
        "'varchar(9)', and SQLite cannot change a column's type in place. "
        + SQLITE_IN_PLACE_FIX
    ]


@sqlite_only
def test_between_two_snapshots_the_old_type_is_the_one_the_create_pass_writes(
    project, pkg, db
):
    declare(project, pkg, NOTE.format(field="count: int"))
    before = compile_registry_schema_ir()
    declare(project, pkg, NOTE.format(field="count: str"))
    after = compile_registry_schema_ir()

    plan = json.loads(
        _core._plan_from_ir(
            json.dumps(before),
            json.dumps(after),
            "sqlite",
            '{"destructive": true}',
            True,
        )
    )

    assert [r["text"] for op in plan["operations"] for r in op["reports"]] == [
        "Column 'r997note.count' has type 'integer' but the model expects "
        "'varchar', and SQLite cannot change a column's type in place. "
        + SQLITE_IN_PLACE_FIX
    ]


@sqlite_only
def test_adopting_migrations_at_what_the_database_holds_makes_the_change(
    project, pkg, db
):
    """The second fix the sentence names, run on an untracked database."""
    db.execute(
        'CREATE TABLE "r997note" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
        '"count" integer NOT NULL)'
    )
    db.execute('INSERT INTO "r997note" ("count") VALUES (7)')
    write_config(project, pkg, '["sqlite"]')
    declare(project, pkg, NOTE.format(field="count: str"))
    assert in_place_texts(migrate(db))[0].endswith(SQLITE_IN_PLACE_FIX)

    steps = [
        (NOTE.format(field="count: int"), ("migrate", "new", "adopt")),
        (None, ("migrate", "baseline", "--url", db.url)),
        (NOTE.format(field="count: str"), ("migrate", "new", "count_text")),
        (None, ("migrate", "up", "--url", db.url)),
    ]
    for models, argv in steps:
        if models is not None:
            write_models(project, pkg, models)
        code, output = cli(*argv)
        assert code == 0, (argv, output)

    (table,) = db.rows("SELECT sql FROM sqlite_master WHERE name = 'r997note'")
    assert '"count" varchar NOT NULL' in table[0], table
    assert db.rows('SELECT "count" FROM "r997note"') == [("7",)]


@sqlite_only
@pytest.mark.parametrize(
    ("ddl", "models"),
    [
        pytest.param(
            '"age" integer',
            """
            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                age: int | None = None

                __ferro_checks__ = (
                    Check("age_positive", lambda r997note: r997note.age > 0),
                )
            """,
            id="missing table check",
        ),
        pytest.param(
            '"mood" text',
            """
            class R997Mood(StrEnum):
                UP = "up"

            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                mood: Annotated[
                    R997Mood | None, FerroField(db_type="text", db_check=True)
                ] = None
            """,
            id="missing column check",
        ),
        pytest.param(
            '"age" integer, CONSTRAINT "ck_r997note_age_positive" CHECK ("age" > 1)',
            """
            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                age: int | None = None

                __ferro_checks__ = (
                    Check("age_positive", lambda r997note: r997note.age > 0),
                )
            """,
            id="check body differs",
        ),
        pytest.param(
            '"age" integer, CONSTRAINT "ck_r997note_age_positive" CHECK ("age" > 0)',
            NOTE.format(field="age: int | None = None"),
            id="check no longer declared",
        ),
        pytest.param(
            '"owner_id" integer',
            """
            class R997Owner(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None

            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                owner_id: int | None = Field(default=None, foreign_key=R997Owner)
            """,
            id="foreign key missing",
        ),
        pytest.param(
            '"owner_id" integer, CONSTRAINT "fk_r997note_owner_id_r997owner" '
            'FOREIGN KEY ("owner_id") REFERENCES "r997owner" ("id")',
            """
            class R997Owner(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None

            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                owner_id: int | None = None
            """,
            id="foreign key no longer declared",
        ),
        pytest.param(
            '"owner_id" integer, CONSTRAINT "fk_r997note_owner_id_r997owner" '
            'FOREIGN KEY ("owner_id") REFERENCES "r997owner" ("id")',
            """
            class R997Owner(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None

            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                owner_id: int | None = Field(
                    default=None, foreign_key=R997Owner, on_delete="SET NULL"
                )
            """,
            id="on_delete differs",
        ),
        pytest.param(
            '"age" integer',
            """
            class R997Owner(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None

            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                age: int | None = None
                owner_id: int = Field(default=1, foreign_key=R997Owner)
            """,
            id="NOT NULL foreign-key column added",
        ),
        pytest.param(
            '"amount" integer NOT NULL',
            """
            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                amount: int
                direction: int = Field(generated=lambda r997note: fn.sign(r997note.amount))
            """,
            id="generated column missing",
        ),
        pytest.param(
            '"amount" integer NOT NULL, "direction" bigint NOT NULL',
            """
            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                amount: int
                direction: int = Field(generated=lambda r997note: fn.sign(r997note.amount))
            """,
            id="plain declared generated",
        ),
        pytest.param(
            '"amount" integer NOT NULL, "direction" bigint NOT NULL '
            'GENERATED ALWAYS AS (sign("amount")) STORED',
            """
            class R997Note(Model):
                id: Annotated[int | None, FerroField(primary_key=True)] = None
                amount: int
                direction: int
            """,
            id="generated declared plain",
        ),
        pytest.param(
            '"age" integer, "old" integer, '
            'CONSTRAINT "ck_r997note_old" CHECK ("old" > 0)',
            NOTE.format(field="age: int | None = None"),
            id="undeclared column held by a check",
        ),
        pytest.param(
            '"age" integer',
            NOTE.format(field="age: int"),
            id="nullability",
        ),
    ],
)
def test_every_in_place_warning_ends_in_the_one_fix(project, pkg, db, ddl, models):
    db.execute('CREATE TABLE "r997owner" ("id" integer PRIMARY KEY AUTOINCREMENT)')
    db.execute(
        f'CREATE TABLE "r997note" ("id" integer PRIMARY KEY AUTOINCREMENT, {ddl})'
    )
    declare(project, pkg, models)

    texts = in_place_texts(migrate(db))

    assert texts
    for text in texts:
        assert text.endswith(". " + SQLITE_IN_PLACE_FIX), text
        assert text.count("ferro migrate new") == 1, text


@pytest.mark.asyncio
async def test_a_database_ferro_created_carries_no_type_name_fact(project, pkg, db):
    declare(
        project,
        pkg,
        """
        import datetime
        import decimal
        import uuid

        class R997Mood(StrEnum):
            UP = "up"

        class R997Owner(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None

        class R997Note(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            owner_id: int | None = Field(default=None, foreign_key=R997Owner)
            count: int
            ratio: float
            flag: bool
            title: str
            code: str = Field(max_length=8)
            body: Annotated[str, FerroField(db_type="text")]
            prefs: dict
            blob: bytes
            at: datetime.datetime
            day: datetime.date
            clock: datetime.time
            key: uuid.UUID
            price: decimal.Decimal
            mood: R997Mood
            direction: int = Field(generated=lambda r997note: fn.sign(r997note.count))
        """,
    )
    await ferro.connect(db.url, auto_migrate=True)
    try:
        _, facts = await _core._live_schema_ir(
            None, json.dumps(compile_registry_schema_ir())
        )
    finally:
        ferro.reset_engine()

    tables = json.loads(facts)["tables"]
    assert {"r997note", "r997owner"} <= tables.keys()
    assert all("foreign_type_names" not in table for table in tables.values())
