"""Row-security reconciliation on live tables (#413, PRD #406).

The create pass owns only missing tables (ADR-0010). This is the other half: a
table that already exists gets brought to its declaration by ``migrate_updates``
— ``ENABLE``/``FORCE`` switched on, missing ``rls_*`` policies created — and the
DDL lands *after* that table's column and data steps, so the migration's own
backfills still see the rows they must touch.

One-way is the rule that matters. Reconciliation can only ever turn row
security **on**. A model that drops its declaration warns on every single
connect and changes nothing; teardown is ``migrate_destructive`` (that ladder,
plus orphans and foreign policies, is ``test_row_security_orphans.py``, and
body drift is ``test_row_security_rebuild.py``).

Live assertions read ``pg_class`` and ``pg_policies`` because that is what
enforces. The enforcement assertion runs as a created ``NOSUPERUSER`` role: the
matrix connects as a superuser, and superusers bypass RLS unconditionally.
"""

import json
import uuid
from typing import ClassVar

import pytest

from ferro import (
    Field,
    Model,
    RowPolicy,
    RowSecurity,
    clear_registry,
    connect,
    engines,
    reset_engine,
)
from ferro._core import (
    _live_row_security_for_test,
    _normalize_row_policy_expr,
    _plan_row_security_reconcile,
    _render_migration_sql_for_test,
)
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all, fetch_one

LEDGER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
LEDGER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")

SETTING = "pinch.ledger_id"
POLICY_NAME = "rls_ledgerrow_ledger_id"
SHORTHAND_EXPR = (
    "\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid"
)
CREATE_POLICY_SQL = (
    f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR ALL '
    f"USING ({SHORTHAND_EXPR}) WITH CHECK ({SHORTHAND_EXPR})"
)
ENABLE_SQL = 'ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY'
FORCE_SQL = 'ALTER TABLE "ledgerrow" FORCE ROW LEVEL SECURITY'


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


def _define_ledger_row(*, force: bool = True, declared: bool = True) -> type[Model]:
    if not declared:

        class LedgerRow(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID
            label: str

        return LedgerRow

    class LedgerRow(Model):  # type: ignore[no-redef]
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=SETTING),
            force=force,
        )

    return LedgerRow


def _define_ledger_row_with_note() -> type[Model]:
    """The same model plus a NOT NULL column with a server default.

    Adding it is a *data-shaped* step: ferro emits an ADD COLUMN that writes a
    value into every existing row. The policy DDL must land after it.
    """

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str
        note: str = Field(default="unset")

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=SETTING),
        )

    return LedgerRow


LEDGER_LIVE_COLUMNS = [
    {
        "name": "id",
        "declared_type": "integer",
        "is_primary_key": True,
        "is_nullable": False,
    },
    {"name": "ledger_id", "declared_type": "uuid", "is_nullable": False},
    {"name": "label", "declared_type": "character varying", "is_nullable": False},
]


def _render(
    live_row_security: dict,
    *,
    dialect: str = "postgres",
    updates: bool = True,
    destructive: bool = False,
    live_columns: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    return _render_migration_sql_for_test(
        "ledgerrow",
        json.dumps(compile_registry_schema_ir()),
        json.dumps(LEDGER_LIVE_COLUMNS if live_columns is None else live_columns),
        dialect,
        updates,
        destructive,
        "",
        "",
        "",
        json.dumps(live_row_security),
    )


def _live_policy(
    name: str = POLICY_NAME,
    *,
    command: str = "all",
    restrictive: bool = False,
    using: str | None = None,
    with_check: str | None = None,
    ferro_owned: bool = True,
) -> dict:
    body = using if using is not None else CATALOG_SHORTHAND
    return {
        "name": name,
        "command": command,
        "restrictive": restrictive,
        "using": body,
        "with_check": with_check if with_check is not None else body,
        "ferro_owned": ferro_owned,
    }


#: Real ``pg_get_expr(polqual, polrelid)`` output for the policy ferro renders
#: on a ``uuid`` column — the text the catalog hands back after ferro's own
#: ``CREATE POLICY``. Everything about phantom drift hangs off this string.
CATALOG_SHORTHAND = (
    "(ledger_id = (NULLIF(current_setting('pinch.ledger_id'::text, true), "
    "''::text))::uuid)"
)


def _model_ir() -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "ledgerrow"
    )


# ---------------------------------------------------------------------------
# Render level
# ---------------------------------------------------------------------------


def test_a_declaration_on_a_live_table_plans_flags_then_the_policy():
    _define_ledger_row()
    statements, warnings = _render({"enabled": False, "forced": False, "policies": []})
    assert statements == [ENABLE_SQL, FORCE_SQL, CREATE_POLICY_SQL]
    assert warnings == []


def test_an_unchanged_declaration_plans_nothing():
    """The heart of the ticket: the catalog's own re-spelling of ferro's
    expression must not read as drift, or every connect would take an
    exclusive lock to rewrite a policy nobody changed."""
    _define_ledger_row()
    statements, warnings = _render(
        {"enabled": True, "forced": True, "policies": [_live_policy()]}
    )
    assert statements == []
    assert warnings == []


def test_flags_are_turned_on_one_at_a_time():
    _define_ledger_row()
    statements, _ = _render(
        {"enabled": True, "forced": False, "policies": [_live_policy()]}
    )
    assert statements == [FORCE_SQL]


def test_force_false_never_clears_a_live_force_on_updates():
    _define_ledger_row(force=False)
    statements, warnings = _render(
        {"enabled": True, "forced": True, "policies": [_live_policy()]}
    )
    assert statements == []
    assert len(warnings) == 1
    assert "force=False" in warnings[0]
    assert "migrate_destructive" in warnings[0]


def test_a_dropped_declaration_never_disables_and_always_warns():
    _define_ledger_row(declared=False)
    statements, warnings = _render(
        {"enabled": True, "forced": True, "policies": [_live_policy()]}
    )
    assert statements == []
    # Two things are true and both are said: the flags are still on, and the
    # policy ferro owned is still filtering.
    assert any("no longer declares __ferro_rls__" in w for w in warnings)
    assert any(POLICY_NAME in w for w in warnings)
    assert all("ledgerrow" in w for w in warnings)


def test_without_migrate_updates_nothing_is_planned():
    _define_ledger_row()
    statements, warnings = _render(
        {"enabled": False, "forced": False, "policies": []}, updates=False
    )
    assert statements == []
    assert warnings == []


def test_sqlite_reconciliation_emits_no_row_security_ddl():
    """ADR-0014: SQLite has no row-level security to reconcile *to*. The one
    warning the create pass already emits stands alone."""
    _define_ledger_row()
    sqlite_columns = [
        {"name": "id", "declared_type": "INTEGER", "is_primary_key": True},
        {"name": "ledger_id", "declared_type": "uuid_text"},
        {"name": "label", "declared_type": "varchar"},
    ]
    statements, warnings = _render(
        {"enabled": False, "forced": False, "policies": []},
        dialect="sqlite",
        live_columns=sqlite_columns,
    )
    assert not any("POLICY" in sql.upper() for sql in statements)
    assert not any("ROW LEVEL SECURITY" in sql.upper() for sql in statements)
    assert [w for w in warnings if "row" in w and "polic" in w] == []


def test_policy_ddl_lands_after_the_tables_own_column_and_data_steps():
    """PRD #406 user story 20. ``note`` is added NOT NULL with a default, which
    writes to every existing row — the migrator must still see those rows, so
    the ADD COLUMN comes first and the policy DDL last."""
    _define_ledger_row_with_note()
    statements, _ = _render({"enabled": False, "forced": False, "policies": []})
    add_column = next(
        index for index, sql in enumerate(statements) if "ADD COLUMN" in sql
    )
    enable = statements.index(ENABLE_SQL)
    create_policy = next(
        index for index, sql in enumerate(statements) if sql.startswith("CREATE POLICY")
    )
    assert add_column < enable < create_policy
    assert '"note"' in statements[add_column]


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_row_security_reconcile_statement_parity_pin():
    """The FFI the Alembic operation (#414) consumes renders the same bytes the
    reconciliation pass executes — one decision, two migration doors."""
    _define_ledger_row()
    live = {"enabled": False, "forced": False, "policies": []}
    plan = json.loads(
        _plan_row_security_reconcile(json.dumps(_model_ir()), json.dumps(live))
    )
    assert plan["missing"] == [POLICY_NAME]
    assert plan["drifted"] == []
    assert plan["statements"] == [ENABLE_SQL, FORCE_SQL, CREATE_POLICY_SQL]

    runtime, _ = _render(live)
    assert plan["statements"] == runtime


def test_the_reconcile_seam_reports_an_unchanged_declaration_as_empty():
    _define_ledger_row()
    plan = json.loads(
        _plan_row_security_reconcile(
            json.dumps(_model_ir()),
            json.dumps({"enabled": True, "forced": True, "policies": [_live_policy()]}),
        )
    )
    assert plan == {
        "statements": [],
        "missing": [],
        "drifted": [],
        "unverifiable": [],
        "extra": [],
        "foreign": [],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Live behavior
# ---------------------------------------------------------------------------


async def _pg_flags(table: str) -> dict:
    row = await fetch_one(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        f"WHERE oid = '\"{table}\"'::regclass"
    )
    assert row is not None
    return row


async def _pg_policies(table: str) -> list[dict]:
    return await fetch_all(
        "SELECT policyname, permissive, cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = $1 ORDER BY policyname",
        table,
    )


@pytest.fixture
def tenant_role():
    """A cluster-unique NOSUPERUSER role name, dropped after the test."""
    return f"ferro_rls_{uuid.uuid4().hex[:12]}"


async def _grant(role: str, table: str) -> None:
    """Create the NOSUPERUSER role with just enough to run the queries.

    Always call this INSIDE the ``try`` whose ``finally`` drops the role: roles
    are cluster-global while the per-test schema is not, so a GRANT that failed
    after CREATE ROLE would leak the role into every later run.
    """
    schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
    await execute(f'CREATE ROLE "{role}" NOSUPERUSER')
    await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    await execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{role}"')
    await execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')


async def _drop_tenant_role(role: str) -> None:
    await execute(f'DROP OWNED BY "{role}"')
    await execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_updates_brings_a_live_table_to_its_declaration(db_url):
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is False
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is True
        policies = await _pg_policies("ledgerrow")
        assert [policy["policyname"] for policy in policies] == [POLICY_NAME]
        assert policies[0]["cmd"] == "ALL"
        assert policies[0]["permissive"] == "PERMISSIVE"


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_second_reconcile_of_the_same_declaration_emits_nothing(db_url):
    """The no-flap test. Reconcile, then reconcile again against what the
    catalog now holds: ferro's rendered expression and the catalog's rewriting
    of it must normalize to the same predicate, or the policy would be dropped
    and recreated on every connect for the rest of its life."""
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        live = await _live_row_security_for_test("ledgerrow")
        catalog_using = live["policies"][0]["using"]
        # Pin the real catalog text against ferro's render, through the one
        # normalizer both sides of the drift decision use.
        assert catalog_using != SHORTHAND_EXPR
        assert _normalize_row_policy_expr(catalog_using) == _normalize_row_policy_expr(
            SHORTHAND_EXPR
        )
        plan = json.loads(
            _plan_row_security_reconcile(json.dumps(_model_ir()), json.dumps(live))
        )
        assert plan["statements"] == []
        assert plan["drifted"] == []
        assert plan["warnings"] == []

    reset_engine()
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        policies = await _pg_policies("ledgerrow")
        assert [policy["policyname"] for policy in policies] == [POLICY_NAME]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_dropping_the_declaration_warns_every_connect_and_changes_nothing(
    db_url, recwarn
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(declared=False)
    for _ in range(2):
        recwarn.clear()
        reset_engine()
        await connect(db_url, migrate_updates=True)
        dropped = [
            w for w in recwarn if "no longer declares __ferro_rls__" in str(w.message)
        ]
        assert len(dropped) == 1, [str(w.message) for w in recwarn]
        async with engines.session():
            flags = await _pg_flags("ledgerrow")
            assert flags["relrowsecurity"] is True
            assert flags["relforcerowsecurity"] is True
            assert [p["policyname"] for p in await _pg_policies("ledgerrow")] == [
                POLICY_NAME
            ]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_destructive_tears_the_declaration_down(db_url, recwarn):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(declared=False)
    recwarn.clear()
    await connect(db_url, migrate_destructive=True)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is False
        assert flags["relforcerowsecurity"] is False
        assert await _pg_policies("ledgerrow") == []
    torn_down = [w for w in recwarn if "tore down row security" in str(w.message)]
    assert len(torn_down) == 1, [str(w.message) for w in recwarn]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_reconciled_declaration_does_not_warn_about_an_unapplied_one(
    db_url, recwarn
):
    """#418 warned that a declaration on a live table was never applied. Now
    that ``migrate_updates`` applies it, that warning must not fire on the very
    connect that fences the table — it still fires when nothing reconciles."""
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    recwarn.clear()
    await connect(db_url, migrate_updates=True)
    unapplied = [w for w in recwarn if "create pass never alters" in str(w.message)]
    assert unapplied == [], [str(w.message) for w in recwarn]

    # …and with reconciliation switched off the gap is reported again.
    _rewind_registry()
    _define_ledger_row(declared=False)
    await connect(db_url, migrate_destructive=True)
    _rewind_registry()

    _define_ledger_row()
    recwarn.clear()
    reset_engine()
    await connect(db_url, auto_migrate=True)
    unapplied = [w for w in recwarn if "create pass never alters" in str(w.message)]
    assert len(unapplied) == 1, [str(w.message) for w in recwarn]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_migrator_warning_is_silent_for_the_superuser_matrix_role(
    db_url, recwarn
):
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    recwarn.clear()
    await connect(db_url, migrate_updates=True)
    filtered = [w for w in recwarn if "BYPASSRLS" in str(w.message)]
    assert filtered == [], [str(w.message) for w in recwarn]


def _url_as_role(db_url: str, role: str, password: str) -> str:
    """The same connection URL, connecting as ``role`` instead."""
    from urllib.parse import quote, urlparse, urlunparse

    parsed = urlparse(db_url)
    host = parsed.hostname or "localhost"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_the_migrator_warning_fires_for_a_bound_role(db_url, tenant_role):
    """PRD #406 user story 19: a migrating role that is neither superuser nor
    BYPASSRLS is filtered by the FORCE policies it maintains, so a backfill can
    see zero rows and report success.

    The matrix connects as a superuser, so proving the warning needs a real
    NOSUPERUSER login role that owns the table and runs the pass itself.
    """
    import warnings as warnings_module

    password = "ferro_rls_probe"
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        schema = (await fetch_one("SELECT current_schema() AS s"))["s"]
        admin = (await fetch_one("SELECT current_user AS u"))["u"]
    try:
        async with engines.session():
            await execute(
                f"CREATE ROLE \"{tenant_role}\" LOGIN NOSUPERUSER PASSWORD '{password}'"
            )
            await execute(f'GRANT ALL ON SCHEMA "{schema}" TO "{tenant_role}"')
            await execute(f'ALTER TABLE "ledgerrow" OWNER TO "{tenant_role}"')
            await execute(
                f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{tenant_role}"'
            )

        _rewind_registry()
        _define_ledger_row()
        role_url = _url_as_role(db_url, tenant_role, password)
        with warnings_module.catch_warnings(record=True) as caught:
            warnings_module.simplefilter("always")
            await connect(role_url, migrate_updates=True)
        filtered = [w for w in caught if "BYPASSRLS" in str(w.message)]
        assert len(filtered) == 1, [str(w.message) for w in caught]
        assert "'ledgerrow'" in str(filtered[0].message)

        # The pass still did its job — the warning is about the migrator's own
        # DML, not a refusal.
        async with engines.session():
            flags = await _pg_flags("ledgerrow")
            assert flags["relforcerowsecurity"] is True
    finally:
        reset_engine()
        _rewind_registry()
        _define_ledger_row(declared=False)
        await connect(db_url)
        async with engines.session():
            await execute(f'ALTER TABLE "ledgerrow" OWNER TO "{admin}"')
            await execute(f'DROP OWNED BY "{tenant_role}"')
            await execute(f'DROP ROLE IF EXISTS "{tenant_role}"')


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_reconciled_policy_actually_filters_rows(db_url, tenant_role):
    """The spot check that the catalog assertions above are not decorative: a
    policy reconciliation installed really does hide another tenant's rows."""
    from ferro import transaction

    LedgerRow = _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")
        await LedgerRow.create(ledger_id=LEDGER_B, label="b1")
    _rewind_registry()

    LedgerRow = _define_ledger_row()
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        try:
            await _grant(tenant_role, "ledgerrow")
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                # No setting: fail closed, and not an error.
                assert await tx.fetch_all("SELECT label FROM ledgerrow") == []
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{tenant_role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                assert [row.label for row in await LedgerRow.all()] == ["a1"]
        finally:
            await _drop_tenant_role(tenant_role)
