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
    _render_migration_sql_for_test,
    _render_table_check_body,
)
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all
from tests._alembic_harness import autogen_upgrade_code as _autogen_upgrade_code
from tests._alembic_harness import planner_statements as _planner_statements
from tests._pass_harness import auto_migrate, schema_steps, warning_texts

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
    assert (statements, warnings) == (
        [
            'ALTER TABLE "rebuild" DROP CONSTRAINT "ck_rebuild_at_most_one_side"',
            'ALTER TABLE "rebuild" ADD CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) AND ("right" IS NULL))',
        ],
        [],
    )
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
        assert (statements, warnings) == ([], [])
        assert statements == [], dialect
        assert warnings == [], dialect


def test_second_boot_shape_replans_to_nothing():
    _define_rebuild(both=False)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert (statements, warnings) == ([], [])
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
    assert (statements, _) == ([], [])
    assert statements == []
    assert not any(SIDE_CHECK_NAME in sql for sql in statements)


def test_sqlite_warns_with_the_constraint_name_and_emits_no_sql():
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements, warnings = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "sqlite")
    assert (statements, warnings) == (
        [],
        [
            "CHECK constraint 'ck_rebuild_at_most_one_side' on table 'rebuild' has a declared body that differs from the live constraint, and SQLite cannot alter constraints in place (it requires a full table rebuild). The live body remains; generate a reviewed migration with `ferro migrate new` to apply the declared predicate.",
        ],
    )
    assert statements == []
    assert len(warnings) == 1
    assert SIDE_CHECK_NAME in warnings[0]
    assert "ferro migrate new" in warnings[0]


def test_without_migrate_updates_no_rebuild_is_planned():
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    for dialect in ("postgres", "sqlite"):
        statements, warnings = _render(
            "rebuild", REBUILD_LIVE_COLUMNS, live, dialect, updates=False
        )
        assert (statements, warnings) == ([], [])
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
    assert (statements, _) == (
        [
            'ALTER TABLE "cookie" DROP CONSTRAINT "ck_cookie_flavor"',
            "ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty', 'umami'))",
        ],
        [],
    )
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
    assert (statements, _) == (
        [],
        [
            "Table 'rebuild' has CHECK constraint(s) 'ck_rebuild_orphan' that the model no longer declares. Leftover CHECKs keep rejecting rows the model now allows. They stay in place unless you pass migrate_destructive=True (Postgres) or drop them with a reviewed migration (`ferro migrate new`).",
        ],
    )
    assert statements == []
    assert not any("ck_rebuild_orphan" in sql for sql in statements)


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_check_rebuild_statement_parity_pin():
    """The planner the Alembic bridge translates renders the same bytes the
    reconciliation pass executes."""
    _define_rebuild(both=True)
    live = [_live_check(SIDE_CHECK_NAME, f"CHECK ({SIDE_CHECK_BODY})")]
    statements = _planner_statements("rebuild", live, destructive=True)
    assert statements == [SIDE_CHECK_DROP, SIDE_CHECK_ADD_AND]

    runtime, _ = _render("rebuild", REBUILD_LIVE_COLUMNS, live, "postgres")
    assert (runtime, _) == (
        [
            'ALTER TABLE "rebuild" DROP CONSTRAINT "ck_rebuild_at_most_one_side"',
            'ALTER TABLE "rebuild" ADD CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) AND ("right" IS NULL))',
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Rebuild.create(left=None, right=None)  # passes both OR and AND
    _rewind_registry()

    Rebuild = _define_rebuild(both=True)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "rebuild",
            'ALTER TABLE "rebuild" DROP CONSTRAINT "ck_rebuild_at_most_one_side"',
        ),
        (
            "rebuild",
            'ALTER TABLE "rebuild" ADD CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) AND ("right" IS NULL))',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    async with engines.session():
        assert await _pg_constraintdef("rebuild", SIDE_CHECK_NAME) == catalog


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_second_migrate_updates_boot_is_a_noop(db_url, recwarn):
    Rebuild = _define_rebuild(both=False)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Rebuild.create(left="a", right=None)
    _rewind_registry()

    _define_rebuild(both=False)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    recwarn.clear()

    _rewind_registry()
    _define_rebuild(both=False)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Rebuild.create(left="a", right=None)  # passes OR, fails AND
    _rewind_registry()

    _define_rebuild(both=True)
    with pytest.raises(CheckViolationError) as raised:
        await auto_migrate(db_url, updates=True)

    # The DROP CONSTRAINT and the failing ADD CONSTRAINT ran in one
    # transaction and rolled back: nothing committed, the ADD is named.
    assert schema_steps(raised.value.report) == []
    assert warning_texts(raised.value.report) == []
    assert (
        f'ALTER TABLE "rebuild" ADD CONSTRAINT "{SIDE_CHECK_NAME}" CHECK '
        '(("left" IS NULL) AND ("right" IS NULL))' in str(raised.value)
    )
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "cookie",
            'CREATE TABLE IF NOT EXISTS "cookie" ( "flavor" text NOT NULL, "id" serial PRIMARY KEY NOT NULL )',
        ),
        (
            "cookie",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cookie_flavor' AND conrelid = '\"cookie\"'::regclass) THEN ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty')); END IF; END $$",
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Cookie.create(flavor=Flavor.SWEET)
        assert "ck_cookie_flavor" in await _pg_check_names("cookie")
    _rewind_registry()

    Cookie = _define_cookie(FlavorWider)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("cookie", 'ALTER TABLE "cookie" DROP CONSTRAINT "ck_cookie_flavor"'),
        (
            "cookie",
            "ALTER TABLE \"cookie\" ADD CONSTRAINT \"ck_cookie_flavor\" CHECK (\"flavor\" IN ('sweet', 'salty', 'umami'))",
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "CHECK constraint 'ck_rebuild_at_most_one_side' on table 'rebuild' has a declared body that differs from the live constraint, and SQLite cannot alter constraints in place (it requires a full table rebuild). The live body remains; generate a reviewed migration with `ferro migrate new` to apply the declared predicate.",
        ]
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


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_proposes_the_same_drop_and_add_as_the_runtime(
    db_url, postgres_base_url, db_schema_name
):
    Rebuild = _define_rebuild(both=False)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "rebuild",
            'CREATE TABLE IF NOT EXISTS "rebuild" ( "id" serial PRIMARY KEY NOT NULL, "left" varchar, "right" varchar, CONSTRAINT "ck_rebuild_at_most_one_side" CHECK (("left" IS NULL) OR ("right" IS NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Rebuild.create(left="a", right=None)
    _rewind_registry()

    _define_rebuild(both=False)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "DROP CONSTRAINT" not in code, code
    assert SIDE_CHECK_NAME not in code, code


# ---------------------------------------------------------------------------
# Associative chains (#437): a >=3-term `|` / `&` check is one n-ary
# BoolExpr to Postgres and pg_get_constraintdef prints it flat. That grouping
# is catalog noise, not drift: no rebuild on connect, nothing in autogenerate.
# ---------------------------------------------------------------------------

SIDES_CHECK_NAME = "ck_sides_at_least_one_side"


def _define_sides(*, op: str) -> type[Model]:
    if op == "or":
        check = Check(
            "at_least_one_side",
            lambda sides: (
                (sides.a != None)  # noqa: E711
                | (sides.b != None)  # noqa: E711
                | (sides.c != None)  # noqa: E711
                | (sides.d != None)  # noqa: E711
            ),
        )
    else:
        check = Check(
            "at_least_one_side",
            lambda sides: (
                (sides.a != None)  # noqa: E711
                & (sides.b != None)  # noqa: E711
                & (sides.c != None)  # noqa: E711
                & (sides.d != None)  # noqa: E711
            ),
        )

    class Sides(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (check,)

        id: int | None = Field(default=None, primary_key=True)
        a: str | None = None
        b: str | None = None
        c: str | None = None
        d: str | None = None

    return Sides


async def _pg_constraint_oid(table: str, name: str) -> str:
    rows = await fetch_all(
        "SELECT oid::text AS oid FROM pg_constraint "
        f"WHERE conrelid = '\"{table}\"'::regclass AND conname = '{name}'"
    )
    assert rows, f"constraint {name} missing on {table}"
    return rows[0]["oid"]


@pytest.mark.parametrize("op", ["or", "and"])
@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_four_term_chain_is_not_rebuilt_on_every_connect(db_url, recwarn, op):
    """Real ``pg_get_constraintdef`` for a four-term chain: flat n-ary in the
    catalog, left-nested in ferro's rendering. Same predicate, so the second
    ``migrate_updates`` boot keeps the very same constraint (same oid)."""
    _define_sides(op=op)
    report = await auto_migrate(db_url)
    body = {
        "and": '((("a" IS NOT NULL) AND ("b" IS NOT NULL)) AND ("c" IS NOT NULL)) '
        'AND ("d" IS NOT NULL)',
        "or": '((("a" IS NOT NULL) OR ("b" IS NOT NULL)) OR ("c" IS NOT NULL)) '
        'OR ("d" IS NOT NULL)',
    }[op]
    assert schema_steps(report) == [
        (
            "sides",
            'CREATE TABLE IF NOT EXISTS "sides" ( "a" varchar, "b" varchar, '
            '"c" varchar, "d" varchar, "id" serial PRIMARY KEY NOT NULL, '
            f'CONSTRAINT "{SIDES_CHECK_NAME}" CHECK ({body}) )',
        )
    ]
    assert warning_texts(report) == []
    async with engines.session():
        catalog = await _pg_constraintdef("sides", SIDES_CHECK_NAME)
        first_oid = await _pg_constraint_oid("sides", SIDES_CHECK_NAME)
    predicate = json.dumps(_model_ir("sides")["table_checks"][0]["predicate"])
    canonical = _render_table_check_body(predicate)
    keyword = op.upper()
    assert catalog.count(keyword) == 3 and canonical.count(keyword) == 3
    assert catalog.count("(") < canonical.count("(") + 2, (
        "the pin requires Postgres to have flattened the chain"
    )

    _rewind_registry()
    _define_sides(op=op)
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    assert not [w for w in recwarn if "ck_sides" in str(w.message)]
    async with engines.session():
        assert await _pg_constraint_oid("sides", SIDES_CHECK_NAME) == first_oid, (
            "the same predicate must not be rebuilt on connect"
        )
        assert await _pg_constraintdef("sides", SIDES_CHECK_NAME) == catalog


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_for_a_four_term_chain(
    db_url, postgres_base_url, db_schema_name
):
    _define_sides(op="or")
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "sides",
            'CREATE TABLE IF NOT EXISTS "sides" ( "a" varchar, "b" varchar, "c" varchar, "d" varchar, "id" serial PRIMARY KEY NOT NULL, CONSTRAINT "ck_sides_at_least_one_side" CHECK (((("a" IS NOT NULL) OR ("b" IS NOT NULL)) OR ("c" IS NOT NULL)) OR ("d" IS NOT NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    _rewind_registry()

    _define_sides(op="or")
    await connect(db_url)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "DROP CONSTRAINT" not in code, code
    assert SIDES_CHECK_NAME not in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_changing_a_chain_term_still_rebuilds(db_url):
    """Flattening must not hide a real change: OR chain -> AND chain is drift."""
    Sides = _define_sides(op="or")
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "sides",
            'CREATE TABLE IF NOT EXISTS "sides" ( "a" varchar, "b" varchar, "c" varchar, "d" varchar, "id" serial PRIMARY KEY NOT NULL, CONSTRAINT "ck_sides_at_least_one_side" CHECK (((("a" IS NOT NULL) OR ("b" IS NOT NULL)) OR ("c" IS NOT NULL)) OR ("d" IS NOT NULL)) )',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        await Sides.create(a="x", b="x", c="x", d="x")
    _rewind_registry()

    Sides = _define_sides(op="and")
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("sides", 'ALTER TABLE "sides" DROP CONSTRAINT "ck_sides_at_least_one_side"'),
        (
            "sides",
            'ALTER TABLE "sides" ADD CONSTRAINT "ck_sides_at_least_one_side" CHECK (((("a" IS NOT NULL) AND ("b" IS NOT NULL)) AND ("c" IS NOT NULL)) AND ("d" IS NOT NULL))',
        ),
    ]
    assert warning_texts(report) == []
    async with engines.session():
        definition = await _pg_constraintdef("sides", SIDES_CHECK_NAME)
        assert "AND" in definition and "OR" not in definition
        with pytest.raises(CheckViolationError) as excinfo:
            await Sides.create(a="x", b=None, c="x", d="x")
        assert excinfo.value.constraint == SIDES_CHECK_NAME
