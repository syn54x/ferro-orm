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
from ferro._core import _plan_check_addition, _render_migration_sql_for_test
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all

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
    assert statements == [SIDE_CHECK_ADD]
    assert warnings == []


def test_live_table_check_replans_to_nothing():
    """No phantom add: a reconciled table produces no DDL on either dialect."""
    _define_reconcile_with_check()
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render("reconcile", RECONCILE_LIVE_COLUMNS, live, dialect)
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
    assert statements == [SIDE_CHECK_ADD]
    assert not any("reconcile_left_not_blank" in sql for sql in statements)


def test_sqlite_warns_with_the_constraint_name_and_emits_no_sql():
    """ADR-0014: no ALTER, no table rebuild — a loud skip."""
    _define_reconcile_with_check()
    statements, warnings = _render("reconcile", RECONCILE_LIVE_COLUMNS, [], "sqlite")
    assert statements == []
    assert len(warnings) == 1
    assert SIDE_CHECK_NAME in warnings[0]
    assert "Alembic" in warnings[0], "the warning names the reviewed-migration exit"


def test_without_migrate_updates_no_check_is_planned():
    """ADR-0010: DDL against an existing table is the reconciliation pass's."""
    _define_reconcile_with_check()
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "reconcile", RECONCILE_LIVE_COLUMNS, [], dialect, updates=False
        )
        assert statements == [], dialect
        assert warnings == [], dialect


def test_toggling_db_check_on_an_existing_column_adds_the_column_check():
    _define_cookie(db_check=True)
    live_columns = [
        {"name": "id", "declared_type": "integer", "is_primary_key": True, "is_nullable": False},
        {"name": "flavor", "declared_type": "text", "is_nullable": False},
    ]
    statements, _ = _render("cookie", live_columns, [], "postgres")
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
    assert len(statements) == 2, statements
    assert 'ADD COLUMN "right"' in statements[0]
    assert statements[1] == SIDE_CHECK_ADD


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_check_addition_statement_parity_pin():
    """The FFI the Alembic comparator consumes renders the same bytes the
    reconciliation pass executes. If either side drifts, the two migration
    doors would run different SQL for the same model."""
    _define_reconcile_with_check()
    model_ir = next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "reconcile"
    )
    plan = json.loads(_plan_check_addition("reconcile", json.dumps(model_ir), []))
    assert plan["names"] == [SIDE_CHECK_NAME]
    assert plan["statements"] == [SIDE_CHECK_ADD]

    runtime, _ = _render("reconcile", RECONCILE_LIVE_COLUMNS, [], "postgres")
    assert plan["statements"] == runtime


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
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    Reconcile = _define_reconcile_with_check()
    await connect(db_url, migrate_updates=True)
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
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    _define_reconcile_with_check()
    await connect(db_url, migrate_updates=True)
    _rewind_registry()
    recwarn.clear()

    _define_reconcile_with_check()
    await connect(db_url, migrate_updates=True)
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
    await connect(db_url, auto_migrate=True)
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

    with pytest.raises(CheckViolationError):
        await connect(db_url, migrate_updates=True)

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
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Cookie.create(flavor=Flavor.SWEET)
        assert await _pg_check_names("cookie") == set()
    _rewind_registry()

    _define_cookie(db_check=True)
    await connect(db_url, migrate_updates=True)
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

    await connect(db_url, auto_migrate=True)
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

    await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert "ck_split_at_most_one_side" in await _pg_check_names("split")
        with pytest.raises(CheckViolationError):
            await Split.create(left="a", right="b")


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_reconcile_warns_with_the_constraint_name_and_adds_nothing(db_url):
    Reconcile = _define_reconcile_without_check()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Reconcile.create(left="a", right="b")
    _rewind_registry()

    Reconcile = _define_reconcile_with_check()
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME) as record:
        await connect(db_url, migrate_updates=True)
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
    """ADR-0010: the create pass owns the table it just built. Its
    backend-limitation warning fires once for the run, not once per pass."""
    _define_cookie(db_check=True)
    await connect(db_url, migrate_updates=True)
    named = [w for w in recwarn if "ck_cookie_flavor" in str(w.message)]
    assert len(named) == 1, [str(w.message) for w in named]


# ---------------------------------------------------------------------------
# Alembic autogenerate (the reviewed-migration door)
# ---------------------------------------------------------------------------


def _autogen_upgrade_code(postgres_base_url, db_schema_name) -> str:
    """Run real autogenerate against the live per-test schema and render the
    upgrade code (mirrors tests/test_label_addition.py's connection dance)."""
    import sqlalchemy as sa
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    metadata = get_metadata()
    for scheme in ("postgresql://", "postgres://"):
        if postgres_base_url.startswith(scheme):
            sync_url = "postgresql+psycopg://" + postgres_base_url[len(scheme) :]
            break
    else:
        sync_url = postgres_base_url
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            script = produce_migrations(ctx, metadata)
        return render_python_code(script.upgrade_ops)
    finally:
        engine.dispose()


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
    await connect(db_url, auto_migrate=True)
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
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Reconcile.create(left="a", right=None)
    _rewind_registry()

    _define_reconcile_with_check()
    await connect(db_url, migrate_updates=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "ADD CONSTRAINT" not in code, code
    assert SIDE_CHECK_NAME not in code, code
