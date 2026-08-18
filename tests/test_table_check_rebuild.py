"""Check-body rebuild (#344): ``migrate_updates`` rebuilds a same-named CHECK
whose live catalog body drifted from the model's canonical rendering.

ADR-0015 is one comparison: ferro's rendered CHECK body versus
``pg_get_constraintdef``, both through one normalizer. Extra wrapping parens,
identifier quotes, and whitespace are not drift. ADR-0014 makes the pass
Postgres-only: SQLite warns with the constraint name and leaves the live body.

Adding a missing name is #343; dropping an orphaned ``ck_*`` is #345. Neither
is exercised here.
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
from ferro._core import (
    _plan_check_rebuild,
    _render_migration_sql_for_test,
    _render_table_check_body,
)
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all

SIDE_CHECK_NAME = "ck_rebuild_at_most_one_side"
SIDE_CHECK_BODY = '("left" IS NULL) OR ("right" IS NULL)'
SIDE_CHECK_BODY_AND = '("left" IS NULL) AND ("right" IS NULL)'
SIDE_CHECK_DROP = f'ALTER TABLE "rebuild" DROP CONSTRAINT "{SIDE_CHECK_NAME}"'
SIDE_CHECK_ADD = f'ALTER TABLE "rebuild" ADD CONSTRAINT "{SIDE_CHECK_NAME}" CHECK ({SIDE_CHECK_BODY})'
SIDE_CHECK_ADD_AND = f'ALTER TABLE "rebuild" ADD CONSTRAINT "{SIDE_CHECK_NAME}" CHECK ({SIDE_CHECK_BODY_AND})'


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


class FlavorWider(StrEnum):
    SWEET = "sweet"
    SALTY = "salty"
    UMAMI = "umami"


def _or_check() -> Check:
    return Check(
        "at_most_one_side",
        lambda rebuild: (rebuild.left == None)  # noqa: E711
        | (rebuild.right == None),  # noqa: E711
    )


def _and_check() -> Check:
    return Check(
        "at_most_one_side",
        lambda rebuild: (rebuild.left == None)  # noqa: E711
        & (rebuild.right == None),  # noqa: E711
    )


def _define_rebuild(*, both: bool) -> type[Model]:
    check = _and_check() if both else _or_check()

    class Rebuild(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (check,)

        id: int | None = Field(default=None, primary_key=True)
        left: str | None = None
        right: str | None = None

    return Rebuild


def _define_cookie(flavor_enum: type[StrEnum]) -> type[Model]:
    class Cookie(Model):
        id: int | None = Field(default=None, primary_key=True)
        flavor: Annotated[flavor_enum, Field(db_type="text", db_check=True)]

    return Cookie


REBUILD_LIVE_COLUMNS = [
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


def _model_ir(table: str) -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == table
    )


# ---------------------------------------------------------------------------
# Render level
# ---------------------------------------------------------------------------


def test_drifted_table_check_renders_drop_then_bare_add_on_postgres():
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert statements == [SIDE_CHECK_DROP, SIDE_CHECK_ADD_AND]
    assert warnings == []
    assert not any("DO $$" in sql for sql in statements)


def test_catalog_wrapping_is_not_drift():
    """pg_get_constraintdef extra parens / unquoted idents are a no-op."""
    _define_rebuild(both=False)
    live = [
        _live_check(
            SIDE_CHECK_NAME,
            "CHECK (((left IS NULL) OR (right IS NULL)))",
        )
    ]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, dialect)
        assert statements == [], dialect
        assert warnings == [], dialect


def test_second_boot_shape_replans_to_nothing():
    _define_rebuild(both=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert statements == []
    assert warnings == []


def test_user_owned_live_check_is_never_rebuilt():
    _define_rebuild(both=True)
    live = [
        _live_check(
            SIDE_CHECK_NAME,
            f"CHECK ({SIDE_CHECK_BODY})",
            ferro_owned=False,
        )
    ]
    statements, _ = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert statements == []
    assert not any(SIDE_CHECK_NAME in sql for sql in statements)


def test_sqlite_warns_with_the_constraint_name_and_emits_no_sql():
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "sqlite")
    assert statements == []
    assert len(warnings) == 1
    assert SIDE_CHECK_NAME in warnings[0]
    assert "Alembic" in warnings[0]


def test_without_migrate_updates_no_rebuild_is_planned():
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "rebuild", REBUILD_LIVE_COLUMNS, live, dialect, updates=False
        )
        assert statements == [], dialect
        assert warnings == [], dialect


def test_column_check_label_change_renders_drop_then_bare_add():
    _define_cookie(FlavorWider)
    live_columns = [
        {
            "name": "id",
            "declared_type": "integer",
            "is_primary_key": True,
            "is_nullable": False,
        },
        {"name": "flavor", "declared_type": "text", "is_nullable": False},
    ]
    live = [_live_check("ck_cookie_flavor", "CHECK (\"flavor\" IN ('sweet', 'salty'))")]
    statements, _ = _render("cookie", live_columns, live, "postgres")
    assert statements == [
        'ALTER TABLE "cookie" DROP CONSTRAINT "ck_cookie_flavor"',
        'ALTER TABLE "cookie" ADD CONSTRAINT "ck_cookie_flavor" '
        "CHECK (\"flavor\" IN ('sweet', 'salty', 'umami'))",
    ]
    assert not any("DO $$" in sql for sql in statements)


def test_undeclared_live_ck_is_not_a_rebuild():
    """Leftovers are #345 — this ticket does not warn-or-remove them."""
    _define_rebuild(both=False)
    live = [
        _live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})"),
        _live_check("ck_rebuild_orphan", "CHECK (true)"),
    ]
    statements, _ = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert statements == []
    assert not any("ck_rebuild_orphan" in sql for sql in statements)


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_check_rebuild_statement_parity_pin():
    """The FFI the Alembic comparator consumes renders the same bytes the
    reconciliation pass executes."""
    _define_rebuild(both=True)
    live = [(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    plan = json.loads(
        _plan_check_rebuild("rebuild", json.dumps(_model_ir("rebuild")), live)
    )
    assert plan["names"] == [SIDE_CHECK_NAME]
    assert plan["statements"] == [SIDE_CHECK_DROP, SIDE_CHECK_ADD_AND]

    runtime, _ = _render(
        "rebuild",
        REBUILD_LIVE_COLUMNS,
        [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")],
        "postgres",
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


async def _pg_constraintdef(table: str, name: str) -> str:
    rows = await fetch_all(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        f"WHERE conrelid = '\"{table}\"'::regclass AND conname = '{name}'"
    )
    assert rows, f"constraint {name} missing on {table}"
    return rows[0]["definition"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_updates_rebuilds_a_drifted_table_check(db_url):
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left=None, right=None)  # passes both OR and AND
    _rewind_registry()

    Rebuild = _define_rebuild(both=True)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert SIDE_CHECK_NAME in await _pg_check_names("rebuild")
        definition = await _pg_constraintdef("rebuild", SIDE_CHECK_NAME)
        assert "AND" in definition
        assert "OR" not in definition
        with pytest.raises(CheckViolationError) as excinfo:
            await Rebuild.create(left="a", right=None)
        assert excinfo.value.constraint == SIDE_CHECK_NAME


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_catalog_parens_do_not_phantom_rebuild(db_url):
    """Pin real ``pg_get_constraintdef`` for the IS NULL / OR shape: extra
    wrapping parens are not drift, and a second boot is a no-op."""
    _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        catalog = await _pg_constraintdef("rebuild", SIDE_CHECK_NAME)
    predicate = json.dumps(_model_ir("rebuild")["table_checks"][0]["predicate"])
    canonical = _render_table_check_body(predicate)
    assert catalog.startswith("CHECK ")
    assert catalog != canonical, (
        "the pin requires the catalog wrapping to actually differ"
    )
    assert catalog.count("(") > canonical.count("(")

    _rewind_registry()
    _define_rebuild(both=False)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        assert await _pg_constraintdef("rebuild", SIDE_CHECK_NAME) == catalog


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_second_migrate_updates_boot_is_a_noop(db_url, recwarn):
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left="a", right=None)
    _rewind_registry()

    _define_rebuild(both=False)
    await connect(db_url, migrate_updates=True)
    recwarn.clear()

    _rewind_registry()
    _define_rebuild(both=False)
    await connect(db_url, migrate_updates=True)
    assert not [w for w in recwarn if "ck_rebuild" in str(w.message)]
    async with engines.session():
        assert await _pg_check_names("rebuild") == {SIDE_CHECK_NAME}


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_rows_violating_the_new_body_fail_the_connect_and_keep_the_old_constraint(
    db_url,
):
    """Fail loudly: DROP + ADD share the per-table transaction, so a failing
    ADD leaves the previous constraint in place."""
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left="a", right=None)  # passes OR, fails AND
    _rewind_registry()

    _define_rebuild(both=True)
    with pytest.raises(CheckViolationError):
        await connect(db_url, migrate_updates=True)

    _rewind_registry()
    await connect(db_url)
    async with engines.session():
        assert SIDE_CHECK_NAME in await _pg_check_names("rebuild")
        definition = await _pg_constraintdef("rebuild", SIDE_CHECK_NAME)
        assert "OR" in definition
        assert "AND" not in definition
        rows = await fetch_all('SELECT "left", "right" FROM "rebuild"')
        assert [(row["left"], row["right"]) for row in rows] == [("a", None)]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_changing_column_check_labels_rebuilds_the_constraint(db_url):
    Cookie = _define_cookie(Flavor)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Cookie.create(flavor=Flavor.SWEET)
        assert "ck_cookie_flavor" in await _pg_check_names("cookie")
    _rewind_registry()

    Cookie = _define_cookie(FlavorWider)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        await Cookie.create(flavor=FlavorWider.UMAMI)
        with pytest.raises(CheckViolationError):
            await execute('INSERT INTO "cookie" ("flavor") VALUES (\'sour\')')


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_rebuild_warns_with_the_constraint_name_and_rewrites_nothing(
    db_url,
):
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left="a", right=None)
        before = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rebuild'"
            )
        )[0]["sql"]
    _rewind_registry()

    Rebuild = _define_rebuild(both=True)
    with pytest.warns(UserWarning, match=SIDE_CHECK_NAME) as record:
        await connect(db_url, migrate_updates=True)
    named = [w for w in record if SIDE_CHECK_NAME in str(w.message)]
    assert len(named) == 1, "one warning per drifted constraint"

    async with engines.session():
        after = (
            await fetch_all(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rebuild'"
            )
        )[0]["sql"]
        assert after == before, "no table rebuild, no ALTER"
        # The old OR body still holds; AND was not applied.
        row = await Rebuild.create(left="b", right=None)
        assert row.id is not None


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
async def test_autogenerate_proposes_the_same_drop_and_add_as_the_runtime(
    db_url, postgres_base_url, db_schema_name
):
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left="a", right=None)
    _rewind_registry()

    _define_rebuild(both=True)
    await connect(db_url)  # no auto-migrate: the database stays drifted

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert SIDE_CHECK_DROP in code, code
    assert SIDE_CHECK_ADD_AND in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_once_the_body_matches(
    db_url, postgres_base_url, db_schema_name
):
    Rebuild = _define_rebuild(both=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await Rebuild.create(left="a", right=None)
    _rewind_registry()

    _define_rebuild(both=False)
    await connect(db_url, migrate_updates=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "DROP CONSTRAINT" not in code, code
    assert SIDE_CHECK_NAME not in code, code
