"""Check addition (#343): ``migrate_updates`` adds a declared CHECK constraint
that the live table is missing — a table check or a column check alike.

ADR-0013 puts every ferro-owned ``ck_*`` in the reconciliation pass. ADR-0014
makes the pass Postgres-only: SQLite can only carry a table constraint from
``CREATE TABLE``, so an existing table warns with the constraint name and is
left alone. Existing rows that violate the new CHECK fail the connect, and the
table's whole plan rolls back (Postgres per-table transaction).

Body drift of a same-named CHECK is a constraint rebuild (#344); dropping an
orphaned ``ck_*`` is #345. Neither is exercised here.
"""

import json
from enum import StrEnum
from typing import Annotated, ClassVar

import pytest

from ferro import (
    Check,
    CheckViolationError,
    Field,
    Model,
    clear_registry,
    connect,
    engines,
    reset_engine,
)
from ferro._core import _render_migration_sql_for_test
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all
from tests._alembic_harness import autogen_upgrade_code as _autogen_upgrade_code
from tests._alembic_harness import planner_statements as _planner_statements
from tests._pass_harness import auto_migrate, schema_steps, warning_texts

SIDE_CHECK_NAME = "ck_reconcile_at_most_one_side"
SIDE_CHECK_BODY = '("left" IS NULL) OR ("right" IS NULL)'
SIDE_CHECK_ADD = (
    f'ALTER TABLE "reconcile" ADD CONSTRAINT "{SIDE_CHECK_NAME}" CHECK ({SIDE_CHECK_BODY})'
)


def _rewind_registry() -> None:
    """Drop every registered model so the same table can be redeclared."""
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.fixture(autouse=True)
def cleanup_registry():
    _rewind_registry()
    yield
    _rewind_registry()


# ---------------------------------------------------------------------------
# Model shapes shared by the render-level and live tests
# ---------------------------------------------------------------------------


class Flavor(StrEnum):
    SWEET = "sweet"
    SALTY = "salty"


def _define_reconcile_without_check() -> type[Model]:
    class Reconcile(Model):
        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    return Reconcile


def _define_reconcile_without_right() -> type[Model]:
    """Live table missing ``right`` — the #423 same-revision shape."""

    class Reconcile(Model):
        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None

    return Reconcile


def _define_reconcile_with_check() -> type[Model]:
    class Reconcile(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check(
                "at_most_one_side",
                lambda reconcile: (reconcile.left == None)  # noqa: E711
                | (reconcile.right == None),  # noqa: E711
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    return Reconcile


def _define_cookie(*, db_check: bool) -> type[Model]:
    class Cookie(Model):
        id: int | None = Field(default=None, primary_key=True)
        flavor: Annotated[Flavor, Field(db_type="text", db_check=db_check)] = Flavor.SWEET

    return Cookie


RECONCILE_LIVE_COLUMNS = [
    {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False},
    {"name": "left", "declared_type": "varchar", "is_nullable": True},
    {"name": "right", "declared_type": "varchar", "is_nullable": True},
]


def _render(
    table: str,
    live_columns: list[dict],
    live_checks: list[dict],
    dialect: str,
    *,
    updates: bool = True,
) -> tuple[list[str], list[str]]:
    return _render_migration_sql_for_test(
        table,
        json.dumps(compile_registry_schema_ir()),
        json.dumps(live_columns),
        dialect,
        updates,
        False,
        "",
        "",
        json.dumps(live_checks),
    )


def _live_check(name: str, definition: str, *, ferro_owned: bool = True) -> dict:
    return {"name": name, "definition": definition, "ferro_owned": ferro_owned}


# ---------------------------------------------------------------------------
# Render level: the diff and the DDL, without a database
# ---------------------------------------------------------------------------


def test_missing_table_check_renders_one_alter_add_constraint_on_postgres():
    _define_reconcile_with_check()
    statements, warnings = _render("reconcile", RECONCILE_LIVE_COLUMNS, [], "postgres")
    assert (statements, warnings) == (
        [
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ],
        [],
    )
    assert statements == [SIDE_CHECK_ADD]
    assert warnings == []


def test_live_table_check_replans_to_nothing():
    """No phantom add: a reconciled table produces no DDL on either dialect."""
    _define_reconcile_with_check()
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render("reconcile", RECONCILE_LIVE_COLUMNS, live, dialect)
        assert (statements, warnings) == ([], [])
        assert statements == [], dialect
        assert warnings == [], dialect


def test_user_owned_live_check_is_not_a_counterpart_and_is_never_touched():
    _define_reconcile_with_check()
    live = [
        _live_check(
            "reconcile_left_not_blank", "CHECK ((\"left\" <> ''))", ferro_owned=False
        )
    ]
    statements, _ = _render("reconcile", RECONCILE_LIVE_COLUMNS, live, "postgres")
    assert (statements, _) == (
        [
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ],
        [],
    )
    assert statements == [SIDE_CHECK_ADD]
    assert not any("reconcile_left_not_blank" in sql for sql in statements)


def test_sqlite_warns_with_the_constraint_name_and_emits_no_sql():
    """ADR-0014: no ALTER, no table rebuild — a loud skip."""
    _define_reconcile_with_check()
    statements, warnings = _render("reconcile", RECONCILE_LIVE_COLUMNS, [], "sqlite")
    assert (statements, warnings) == (
        [],
        [
            "Table check 'ck_reconcile_at_most_one_side' is declared on 'reconcile' but missing from the live table, and SQLite cannot add a table constraint to an existing table (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.",
        ],
    )
    assert statements == []
    assert len(warnings) == 1
    assert SIDE_CHECK_NAME in warnings[0]
    assert "ferro migrate new" in warnings[0], "the warning names the Migrations door"
    assert "Alembic" not in warnings[0]


def test_sqlite_column_check_on_an_existing_column_warns_naming_migrations():
    """The column already exists: SQLite cannot attach a constraint to it
    without a table rebuild, so the add is a loud skip naming the door."""
    _define_cookie(db_check=True)
    live_columns = [
        {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False},
        {"name": "flavor", "declared_type": "text", "is_nullable": False},
    ]
    statements, warnings = _render("cookie", live_columns, [], "sqlite")
    assert (statements, warnings) == (
        [],
        [
            "Check constraint 'ck_cookie_flavor' on column 'cookie.flavor' is declared but missing from the live table, and SQLite cannot add a constraint to an existing column (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.",
        ],
    )
    assert statements == []
    assert len(warnings) == 1
    assert "ck_cookie_flavor" in warnings[0]
    assert "ferro migrate new" in warnings[0] and "Alembic" not in warnings[0]


def test_sqlite_column_check_on_a_new_column_rides_its_add_column_inline():
    """#514: SQLite's ADD COLUMN accepts the column CHECK; the check is not
    skipped. (The required column comes in nullable and backfilled: SQLite
    would keep a NOT NULL add's DEFAULT for good, ADR-0027.)"""
    _define_cookie(db_check=True)
    pk_only = [
        {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False}
    ]
    statements, warnings = _render("cookie", pk_only, [], "sqlite")
    assert statements == [
        'ALTER TABLE "cookie" ADD COLUMN "flavor" text'
        " CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty'))",
        'UPDATE "cookie" SET "flavor" = \'sweet\' WHERE "flavor" IS NULL',
    ]
    assert len(warnings) == 1 and "cookie.flavor" in warnings[0], warnings
    assert "ck_cookie_flavor" not in warnings[0], "the check is not what is skipped"


def test_without_migrate_updates_no_check_is_planned():
    """ADR-0010: DDL against an existing table is the reconciliation pass's."""
    _define_reconcile_with_check()
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "reconcile", RECONCILE_LIVE_COLUMNS, [], dialect, updates=False
        )
        assert (statements, warnings) == ([], [])
        assert statements == [], dialect
        assert warnings == [], dialect


def test_toggling_db_check_on_an_existing_column_adds_the_column_check():
    _define_cookie(db_check=True)
    live_columns = [
        {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False},
        {"name": "flavor", "declared_type": "text", "is_nullable": False},
    ]
    statements, _ = _render("cookie", live_columns, [], "postgres")
    assert (statements, _) == (
        [
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cookie_flavor' AND conrelid = '\"cookie\"'::regclass) THEN ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty')); END IF; END $$",
        ],
        [],
    )
    assert len(statements) == 1
    assert "ck_cookie_flavor" in statements[0]
    assert "\"flavor\" IN ('sweet', 'salty')" in statements[0]


def test_column_check_on_a_new_column_rides_its_add_column_exactly_once():
    """``emit_add_column`` already emits the db_check DO-block; the standalone
    add must not duplicate it."""
    _define_cookie(db_check=True)
    pk_only = [
        {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False}
    ]
    statements, _ = _render("cookie", pk_only, [], "postgres")
    assert (statements, _) == (
        [
            'ALTER TABLE "cookie" ADD COLUMN "flavor" text NOT NULL DEFAULT \'sweet\'',
            'ALTER TABLE "cookie" ALTER COLUMN "flavor" DROP DEFAULT',
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cookie_flavor' AND conrelid = '\"cookie\"'::regclass) THEN ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty')); END IF; END $$",
        ],
        [],
    )
    assert 'ADD COLUMN "flavor"' in statements[0]
    adds = [sql for sql in statements if "ADD CONSTRAINT" in sql]
    assert len(adds) == 1, statements
    assert "ck_cookie_flavor" in adds[0]


def test_a_new_column_lands_before_the_check_that_references_it():
    """The single-deploy shape (CONTEXT.md *reconciliation pass*)."""
    _define_reconcile_with_check()
    without_right = [
        column for column in RECONCILE_LIVE_COLUMNS if column["name"] != "right"
    ]
    statements, _ = _render("reconcile", without_right, [], "postgres")
    assert (statements, _) == (
        [
            'ALTER TABLE "reconcile" ADD COLUMN "right" varchar',
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ],
        [],
    )
    assert len(statements) == 2, statements
    assert 'ADD COLUMN "right"' in statements[0]
    assert statements[1] == SIDE_CHECK_ADD


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_check_addition_statement_parity_pin():
    """The planner the Alembic bridge translates renders the same bytes the
    reconciliation pass executes. If either side drifts, the two migration
    doors would run different SQL for the same model."""
    _define_reconcile_with_check()
    statements = _planner_statements("reconcile", [], destructive=True)
    assert statements == [SIDE_CHECK_ADD]

    runtime, _ = _render("reconcile", RECONCILE_LIVE_COLUMNS, [], "postgres")
    assert (runtime, _) == (
        [
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ],
        [],
    )
    assert statements == runtime


# ---------------------------------------------------------------------------
# Live behavior
# ---------------------------------------------------------------------------


async def _pg_check_names(table: str) -> set[str]:
    rows = await fetch_all(
        "SELECT conname FROM pg_constraint "
        f"WHERE conrelid = '\"{table}\"'::regclass AND contype = 'c'"
    )
    return {row["conname"] for row in rows}


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_updates_adds_a_missing_table_check(db_url):
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    Reconcile = _define_reconcile_with_check()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "reconcile",
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        assert SIDE_CHECK_NAME in await _pg_check_names("reconcile")
        # The invariant is enforced from here on.
        with pytest.raises(CheckViolationError) as excinfo:
            await Reconcile.create(left="a", right="b")
        assert excinfo.value.constraint == SIDE_CHECK_NAME


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_second_migrate_updates_boot_is_a_noop(db_url, recwarn):
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    _define_reconcile_with_check()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "reconcile",
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ),
    ]
    assert warning_texts(report) == []
    _rewind_registry()
    recwarn.clear()

    _define_reconcile_with_check()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    assert not [w for w in recwarn if "ck_reconcile" in str(w.message)]
    async with engines.session():
        assert await _pg_check_names("reconcile") == {SIDE_CHECK_NAME}


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_rows_violating_the_new_check_fail_the_connect_and_roll_the_table_back(
    db_url,
):
    """Fail loudly, and leave the table exactly as it was: the added column of
    the same plan must be gone too (FF-G G3's per-table transaction)."""
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right="b")  # violates the new invariant
    _rewind_registry()

    class Reconcile(Model):  # noqa: F811 — the same table, drifted
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check(
                "at_most_one_side",
                lambda reconcile: (reconcile.left == None)  # noqa: E711
                | (reconcile.right == None),  # noqa: E711
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None
        memo: str | None = None

    with pytest.raises(CheckViolationError) as raised:
        await auto_migrate(db_url, updates=True)

    # The ADD COLUMN "memo" and the failing ADD CONSTRAINT ran in one
    # transaction and rolled back: nothing committed, the ADD is named.
    assert schema_steps(raised.value.report) == []
    assert warning_texts(raised.value.report) == []
    assert SIDE_CHECK_ADD in str(raised.value)
    _rewind_registry()
    await connect(db_url)
    async with engines.session():
        assert SIDE_CHECK_NAME not in await _pg_check_names("reconcile")
        columns = await fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'reconcile'"
        )
        assert "memo" not in {row["column_name"] for row in columns}, (
            "the whole table plan rolled back, not just the failing statement"
        )
        rows = await fetch_all('SELECT "left", "right" FROM "reconcile"')
        assert [(row["left"], row["right"]) for row in rows] == [("a", "b")]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_toggling_db_check_on_a_live_column_adds_the_column_check(db_url):
    Cookie = _define_cookie(db_check=False)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "cookie",
            'CREATE TABLE IF NOT EXISTS "cookie" ( "flavor" text NOT NULL, "id" serial PRIMARY KEY NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Cookie.create(flavor=Flavor.SWEET)
        assert await _pg_check_names("cookie") == set()
    _rewind_registry()

    _define_cookie(db_check=True)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "cookie",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cookie_flavor' AND conrelid = '\"cookie\"'::regclass) THEN ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty')); END IF; END $$",
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        assert "ck_cookie_flavor" in await _pg_check_names("cookie")
        with pytest.raises(CheckViolationError):
            await execute("INSERT INTO \"cookie\" (\"flavor\") VALUES ('sour')")


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_new_column_and_a_check_over_it_land_in_one_run(db_url):
    class Split(Model):
        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "split",
            'CREATE TABLE IF NOT EXISTS "split" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Split.create(left="a")
    _rewind_registry()

    class Split(Model):  # noqa: F811 — the same table, one column wider
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check(
                "at_most_one_side",
                lambda split: (split.left == None) | (split.right == None),  # noqa: E711
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("split", 'ALTER TABLE "split" ADD COLUMN "right" varchar'),
        (
            "split",
            'ALTER TABLE "split" ADD CONSTRAINT "ck_split_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        assert "ck_split_at_most_one_side" in await _pg_check_names("split")
        with pytest.raises(CheckViolationError):
            await Split.create(left="a", right="b")


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_reconcile_warns_with_the_constraint_name_and_adds_nothing(db_url):
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right="b")
    _rewind_registry()

    Reconcile = _define_reconcile_with_check()
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME) as record:
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Table check 'ck_reconcile_at_most_one_side' is declared on 'reconcile' but missing from the live table, and SQLite cannot add a table constraint to an existing table (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.",
        ]
    named = [w for w in record if SIDE_CHECK_NAME in str(w.message)]
    assert len(named) == 1, "one warning per missing constraint"

    async with engines.session():
        rows = await fetch_all(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reconcile'"
        )
        assert "CHECK" not in rows[0]["sql"], "no table rebuild, no ALTER"
        # The invariant is not database-enforced, exactly as the warning says.
        row = await Reconcile.create(left="a", right="b")
        assert row.id is not None


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_table_created_in_this_run_is_not_reconciled_again(db_url, recwarn):
    """ADR-0010: the create pass owns the table it just built. On SQLite the
    column check now rides the CREATE TABLE inline (#514), so neither pass
    has anything to warn about."""
    Cookie = _define_cookie(db_check=True)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "cookie",
            'CREATE TABLE IF NOT EXISTS "cookie" ( "flavor" text NOT NULL CONSTRAINT "ck_cookie_flavor" CHECK ("flavor" IN (\'sweet\', \'salty\')), "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )',
        ),
    ]
    assert warning_texts(report) == []
    named = [w for w in recwarn if "ck_cookie_flavor" in str(w.message)]
    assert named == [], [str(w.message) for w in named]
    async with engines.session():
        rows = await fetch_all(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cookie'"
        )
        assert 'CONSTRAINT "ck_cookie_flavor" CHECK' in rows[0]["sql"]
        await Cookie.create(flavor=Flavor.SALTY)
        with pytest.raises(CheckViolationError):
            await execute('INSERT INTO "cookie" ("flavor") VALUES (\'sour\')')


# ---------------------------------------------------------------------------
# Alembic autogenerate (the reviewed-migration door)
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_proposes_the_same_add_as_the_runtime(
    db_url, postgres_base_url, db_schema_name
):
    """I-1: both migration doors name the constraint the same and render the
    same body. Autogenerate is not ``migrate_updates``-gated — running it is
    itself the request for a diff."""
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    _define_reconcile_with_check()
    await connect(db_url)  # no auto-migrate: the database stays drifted

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert SIDE_CHECK_ADD in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_once_the_check_is_reconciled(
    db_url, postgres_base_url, db_schema_name
):
    """No phantom diffs (AGENTS.md § I-1): what the reconciliation pass applied,
    autogenerate does not propose again."""
    Reconcile = _define_reconcile_without_check()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    _define_reconcile_with_check()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "reconcile",
            'ALTER TABLE "reconcile" ADD CONSTRAINT "ck_reconcile_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL))',
        ),
    ]
    assert warning_texts(report) == []

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "ADD CONSTRAINT" not in code, code
    assert SIDE_CHECK_NAME not in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_adds_a_column_before_the_check_that_references_it(
    db_url, postgres_base_url, db_schema_name
):
    """#423: a CHECK over a newly added column must land after ADD COLUMN.

    The live table is missing ``right``. The new model adds that column and a
    table check over it. Rendered upgrade order is the seam: ``op.add_column``
    before the ADD CONSTRAINT SQL (same shape as the RLS create-table-then-policy
    pin).
    """
    Reconcile = _define_reconcile_without_right()
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconcile",
            'CREATE TABLE IF NOT EXISTS "reconcile" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Reconcile.create(left="a")
    _rewind_registry()

    _define_reconcile_with_check()
    await connect(db_url)  # no auto-migrate: the database stays drifted

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "op.add_column(" in code, code
    assert SIDE_CHECK_ADD in code, code
    assert code.index("op.add_column(") < code.index(SIDE_CHECK_ADD), code
