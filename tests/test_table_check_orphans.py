"""Leftover ferro-owned CHECKs (#345): a live ``ck_*`` the model no longer
declares stays under ``migrate_updates`` (with a warning that names it) and
drops under ``migrate_destructive`` on Postgres.

ADR-0013: leftover CHECKs keep rejecting rows the model now allows, so
silence (the index-orphan behavior) is not acceptable. User-created CHECKs
(any other name) are never touched. ADR-0014 makes the drop Postgres-only:
SQLite warns with the constraint name and leaves the live body.

Adding a missing name is #343; same-name body drift is #344. Neither is
exercised here.
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
from ferro._core import _plan_check_drop, _render_migration_sql_for_test
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all

SIDE_CHECK_NAME = "ck_orphan_at_most_one_side"
SIDE_CHECK_BODY = '("left" IS NULL) OR ("right" IS NULL)'
SIDE_CHECK_DROP = f'ALTER TABLE "orphan" DROP CONSTRAINT "{SIDE_CHECK_NAME}"'
USER_CHECK_NAME = "orphan_left_not_blank"
COOKIE_CHECK_NAME = "ck_cookie_flavor"
COOKIE_CHECK_DROP = f'ALTER TABLE "cookie" DROP CONSTRAINT "{COOKIE_CHECK_NAME}"'


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
# Model shapes
# ---------------------------------------------------------------------------


class Flavor(StrEnum):
    SWEET = "sweet"
    SALTY = "salty"


def _or_check() -> Check:
    return Check(
        "at_most_one_side",
        lambda orphan: (
            (orphan.left == None)  # noqa: E711
            | (orphan.right == None)
        ),  # noqa: E711
    )


def _define_orphan(*, with_check: bool) -> type[Model]:
    class Orphan(Model):
        if with_check:
            __ferro_checks__: ClassVar[tuple[Check, ...]] = (_or_check(),)

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    return Orphan


def _define_cookie(*, db_check: bool) -> type[Model]:
    class Cookie(Model):
        id: int | None = Field(default=None, primary_key=True)
        flavor: Annotated[Flavor, Field(db_type="text", db_check=db_check)] = (
            Flavor.SWEET
        )

    return Cookie


ORPHAN_LIVE_COLUMNS = [
    {
        "name": "id",
        "declared_type": "integer",
        "is_primary_key": True,
        "is_nullable": False,
    },
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
    destructive: bool = False,
) -> tuple[list[str], list[str]]:
    return _render_migration_sql_for_test(
        table,
        json.dumps(compile_registry_schema_ir()),
        json.dumps(live_columns),
        dialect,
        updates,
        destructive,
        "",
        "",
        json.dumps(live_checks),
    )


def _live_check(name: str, definition: str, *, ferro_owned: bool = True) -> dict:
    return {"name": name, "definition": definition, "ferro_owned": ferro_owned}


def _model_ir(table: str) -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == table
    )


# ---------------------------------------------------------------------------
# Render level
# ---------------------------------------------------------------------------


def test_leftover_table_check_warns_and_stays_under_migrate_updates():
    _define_orphan(with_check=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render("orphan", ORPHAN_LIVE_COLUMNS, live, dialect)
        assert statements == [], dialect
        assert len(warnings) == 1, (dialect, warnings)
        assert SIDE_CHECK_NAME in warnings[0]
        assert "migrate_destructive" in warnings[0]
        assert "Alembic" in warnings[0]


def test_leftover_table_check_drops_under_migrate_destructive_on_postgres():
    _define_orphan(with_check=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render(
        "orphan", ORPHAN_LIVE_COLUMNS, live, "postgres", destructive=True
    )
    assert statements == [SIDE_CHECK_DROP]
    assert len(warnings) == 1
    assert SIDE_CHECK_NAME in warnings[0]


def test_leftover_table_check_warns_and_skips_on_sqlite_even_when_destructive():
    _define_orphan(with_check=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render(
        "orphan", ORPHAN_LIVE_COLUMNS, live, "sqlite", destructive=True
    )
    assert statements == []
    assert any(SIDE_CHECK_NAME in warning for warning in warnings)
    assert any("Alembic" in warning for warning in warnings)


def test_clearing_db_check_follows_the_same_warn_and_drop_rule():
    _define_cookie(db_check=False)
    live_columns = [
        {
            "name": "id",
            "declared_type": "integer",
            "is_primary_key": True,
            "is_nullable": False,
        },
        {"name": "flavor", "declared_type": "text", "is_nullable": False},
    ]
    live = [_live_check(COOKIE_CHECK_NAME, "CHECK (\"flavor\" IN ('sweet', 'salty'))")]
    statements, warnings = _render("cookie", live_columns, live, "postgres")
    assert statements == []
    assert any(COOKIE_CHECK_NAME in warning for warning in warnings)

    statements, _ = _render("cookie", live_columns, live, "postgres", destructive=True)
    assert statements == [COOKIE_CHECK_DROP]


def test_user_owned_live_check_is_never_warned_or_dropped():
    _define_orphan(with_check=False)
    live = [
        _live_check(
            USER_CHECK_NAME,
            "CHECK ((\"left\" <> ''))",
            ferro_owned=False,
        )
    ]
    for dialect in ("postgres", "sqlite"):
        for destructive in (False, True):
            statements, warnings = _render(
                "orphan",
                ORPHAN_LIVE_COLUMNS,
                live,
                dialect,
                destructive=destructive,
            )
            assert statements == [], (dialect, destructive)
            assert warnings == [], (dialect, destructive, warnings)
            assert not any(USER_CHECK_NAME in sql for sql in statements)


def test_declared_check_is_not_a_leftover():
    _define_orphan(with_check=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "orphan", ORPHAN_LIVE_COLUMNS, live, dialect, destructive=True
        )
        assert statements == [], dialect
        assert warnings == [], dialect


def test_without_migrate_updates_no_leftover_is_planned():
    _define_orphan(with_check=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "orphan",
            ORPHAN_LIVE_COLUMNS,
            live,
            dialect,
            updates=False,
        )
        assert statements == [], dialect
        assert warnings == [], dialect


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_check_drop_statement_parity_pin():
    """The FFI the Alembic comparator consumes renders the same bytes the
    reconciliation pass executes under ``migrate_destructive``."""
    _define_orphan(with_check=False)
    live_names = [SIDE_CHECK_NAME]
    plan = json.loads(
        _plan_check_drop("orphan", json.dumps(_model_ir("orphan")), live_names)
    )
    assert plan["names"] == [SIDE_CHECK_NAME]
    assert plan["statements"] == [SIDE_CHECK_DROP]

    runtime, _ = _render(
        "orphan",
        ORPHAN_LIVE_COLUMNS,
        [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")],
        "postgres",
        destructive=True,
    )
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
async def test_migrate_updates_leaves_a_removed_table_check_and_warns(db_url):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left=None, right=None)
        assert SIDE_CHECK_NAME in await _pg_check_names("orphan")
    _rewind_registry()

    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME) as record:
        await connect(db_url, migrate_updates=True)
    named = [w for w in record if SIDE_CHECK_NAME in str(w.message)]
    assert len(named) == 1
    assert "migrate_destructive" in str(named[0].message)

    async with engines.session():
        assert SIDE_CHECK_NAME in await _pg_check_names("orphan")


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_destructive_drops_the_orphaned_table_check(db_url):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left="a", right=None)
        assert SIDE_CHECK_NAME in await _pg_check_names("orphan")
    _rewind_registry()

    Orphan = _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME):
        await connect(db_url, migrate_destructive=True)
    async with engines.session():
        assert SIDE_CHECK_NAME not in await _pg_check_names("orphan")
        row = await Orphan.create(left="c", right="d")
        assert row.id is not None


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_clearing_db_check_warns_then_drops_on_destructive(db_url):
    Cookie = _define_cookie(db_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Cookie.create(flavor=Flavor.SWEET)
        assert COOKIE_CHECK_NAME in await _pg_check_names("cookie")
    _rewind_registry()

    _define_cookie(db_check=False)
    with pytest.warns(UserWarning, match=COOKIE_CHECK_NAME):
        await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert COOKIE_CHECK_NAME in await _pg_check_names("cookie")

    _rewind_registry()
    Cookie = _define_cookie(db_check=False)
    with pytest.warns(UserWarning, match=COOKIE_CHECK_NAME):
        await connect(db_url, migrate_destructive=True)
    async with engines.session():
        assert COOKIE_CHECK_NAME not in await _pg_check_names("cookie")
        await execute('INSERT INTO "cookie" ("flavor") VALUES (\'sour\')')


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_user_created_non_ck_check_survives_both_flags(db_url, recwarn):
    _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(
            f'ALTER TABLE "orphan" ADD CONSTRAINT "{USER_CHECK_NAME}" '
            "CHECK ((\"left\" IS DISTINCT FROM ''))"
        )
        assert USER_CHECK_NAME in await _pg_check_names("orphan")
    _rewind_registry()

    _define_orphan(with_check=True)
    recwarn.clear()
    await connect(db_url, migrate_updates=True)
    assert not [w for w in recwarn if USER_CHECK_NAME in str(w.message)]
    async with engines.session():
        assert USER_CHECK_NAME in await _pg_check_names("orphan")

    _rewind_registry()
    recwarn.clear()
    _define_orphan(with_check=True)
    await connect(db_url, migrate_destructive=True)
    assert not [w for w in recwarn if USER_CHECK_NAME in str(w.message)]
    async with engines.session():
        assert USER_CHECK_NAME in await _pg_check_names("orphan")
        assert SIDE_CHECK_NAME in await _pg_check_names("orphan")


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_second_updates_boot_does_not_remove_the_leftover(db_url, recwarn):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left=None, right=None)
    _rewind_registry()

    _define_orphan(with_check=False)
    await connect(db_url, migrate_updates=True)
    recwarn.clear()

    _rewind_registry()
    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME):
        await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert SIDE_CHECK_NAME in await _pg_check_names("orphan")


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_leftover_warns_and_rewrites_nothing(db_url):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left=None, right=None)
        before = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orphan'"
            )
        )[0]["sql"]
    _rewind_registry()

    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME) as record:
        await connect(db_url, migrate_updates=True)
    named = [w for w in record if SIDE_CHECK_NAME in str(w.message)]
    assert named, "silence is wrong for leftover CHECKs"

    async with engines.session():
        after = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orphan'"
            )
        )[0]["sql"]
        assert after == before, "no table rebuild, no ALTER"
        # The leftover OR body still holds.
        with pytest.raises(CheckViolationError):
            await execute(
                'INSERT INTO "orphan" ("left", "right") VALUES (\'a\', \'b\')'
            )


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_destructive_still_skips_the_drop(db_url):
    _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        before = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orphan'"
            )
        )[0]["sql"]
    _rewind_registry()

    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME):
        await connect(db_url, migrate_destructive=True)
    async with engines.session():
        after = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orphan'"
            )
        )[0]["sql"]
        assert after == before


# ---------------------------------------------------------------------------
# Alembic autogenerate
# ---------------------------------------------------------------------------


def _autogen_upgrade_code(postgres_base_url, db_schema_name) -> str:
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
async def test_autogenerate_proposes_the_runtime_drop_after_a_non_destructive_connect(
    db_url, postgres_base_url, db_schema_name
):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left=None, right=None)
    _rewind_registry()

    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME):
        await connect(db_url, migrate_updates=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert SIDE_CHECK_DROP in code, code
    assert "import ferro" not in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_once_the_leftover_is_dropped(
    db_url, postgres_base_url, db_schema_name
):
    Orphan = _define_orphan(with_check=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Orphan.create(left=None, right=None)
    _rewind_registry()

    _define_orphan(with_check=False)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME):
        await connect(db_url, migrate_destructive=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert SIDE_CHECK_DROP not in code, code
    assert SIDE_CHECK_NAME not in code, code
