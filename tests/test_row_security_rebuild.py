"""Row-policy drift and rebuild (#413, PRD #406).

A live ``rls_*`` policy that no longer matches its declaration is rebuilt with
``DROP POLICY`` + ``CREATE POLICY`` — metadata only: no row is read, validated,
or rewritten, unlike a CHECK rebuild, which revalidates the whole table.

The decision follows ADR-0015's pattern — ferro's canonical render against the
catalog's stored expression, **both** through one normalizer — with one rule
layered on top:

    ferro rebuilds the bodies it writes and reports the bodies you write.

The column/setting shorthand is ferro's own rendering, and exactly what
Postgres stores for it is pinned below against real ``pg_get_expr`` output, so
a difference there is real drift. A raw ``using=``/``with_check=`` is author
SQL that the server re-spells (``BETWEEN a AND b`` comes back as two
comparisons); ferro cannot tell an edited expression from a re-spelled one, so
it reports the difference with both texts and rebuilds nothing — a rebuild that
re-drifts on the next connect would take an exclusive lock on the table at
every boot, forever.

Command, permissive/restrictive, and which clauses exist are ferro's own
metadata rather than the server's rendering, so they are decided exactly for
both forms.
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
from ferro.raw import execute, fetch_all

LEDGER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
LEDGER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")

SETTING = "pinch.ledger_id"
OTHER_SETTING = "pinch.tenant_id"
POLICY_NAME = "rls_ledgerrow_ledger_id"

DROP_POLICY_SQL = f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"'


def _shorthand_expr(setting: str = SETTING) -> str:
    return f"\"ledger_id\" = NULLIF(current_setting('{setting}', true), '')::uuid"


def _create_policy_sql(
    setting: str = SETTING, *, command: str = "ALL", restrictive: bool = False
) -> str:
    expr = _shorthand_expr(setting)
    sql = f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow"'
    if restrictive:
        sql += " AS RESTRICTIVE"
    sql += f" FOR {command}"
    if command != "INSERT":
        sql += f" USING ({expr})"
    if command in ("ALL", "INSERT", "UPDATE"):
        sql += f" WITH CHECK ({expr})"
    return sql


#: Real ``pg_get_expr(polqual, polrelid)`` output, captured from PostgreSQL
#: after ferro's own ``CREATE POLICY``. Every "no phantom drift" assertion in
#: this file hangs off these two strings differing textually from what ferro
#: wrote while meaning the same thing.
CATALOG_SHORTHAND = (
    "(ledger_id = (NULLIF(current_setting('pinch.ledger_id'::text, true), "
    "''::text))::uuid)"
)
CATALOG_SHORTHAND_OTHER = (
    "(ledger_id = (NULLIF(current_setting('pinch.tenant_id'::text, true), "
    "''::text))::uuid)"
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
# Model shapes
# ---------------------------------------------------------------------------


def _define_ledger_row(
    *, setting: str = SETTING, command: str = "all", restrictive: bool = False
) -> type[Model]:
    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(
                column="ledger_id",
                setting=setting,
                command=command,
                restrictive=restrictive,
            )
        )

    return LedgerRow


def _define_raw_ledger_row(using: str) -> type[Model]:
    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(name="ledger_id", command="select", using=using)
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


def _live(
    *,
    command: str = "all",
    restrictive: bool = False,
    using: str | None = CATALOG_SHORTHAND,
    with_check: str | None = CATALOG_SHORTHAND,
) -> dict:
    return {
        "enabled": True,
        "forced": True,
        "policies": [
            {
                "name": POLICY_NAME,
                "command": command,
                "restrictive": restrictive,
                "using": using,
                "with_check": with_check,
                "ferro_owned": True,
            }
        ],
    }


def _render(live_row_security: dict) -> tuple[list[str], list[str]]:
    return _render_migration_sql_for_test(
        "ledgerrow",
        json.dumps(compile_registry_schema_ir()),
        json.dumps(LEDGER_LIVE_COLUMNS),
        "postgres",
        True,
        False,
        "",
        "",
        "",
        json.dumps(live_row_security),
    )


def _model_ir() -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "ledgerrow"
    )


# ---------------------------------------------------------------------------
# The normalizer, pinned against real catalog output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "live"),
    [
        pytest.param(_shorthand_expr(), CATALOG_SHORTHAND, id="uuid-shorthand"),
        pytest.param(
            "\"owner\" = NULLIF(current_setting('pinch.member', true), '')",
            "(owner = NULLIF(current_setting('pinch.member'::text, true), ''::text))",
            id="text-shorthand",
        ),
        pytest.param(
            "\"title\" = NULLIF(current_setting('pinch.title', true), '')",
            "((title)::text = NULLIF(current_setting('pinch.title'::text, true), "
            "''::text))",
            id="varchar-shorthand-implicit-cast",
        ),
        pytest.param(
            '"id" IN (SELECT doc_id FROM membership WHERE member = '
            "NULLIF(current_setting('pinch.member', true), ''))",
            "(id IN ( SELECT membership.doc_id\n   FROM membership\n  WHERE "
            "(membership.member = NULLIF(current_setting('pinch.member'::text, "
            "true), ''::text))))",
            id="raw-membership-subquery",
        ),
        pytest.param(
            '"owner" = (SELECT probe_uid())',
            "(owner = ( SELECT probe_uid() AS probe_uid))",
            id="raw-function-recipe",
        ),
        pytest.param(
            '"owner" IS NOT NULL AND "n" > 0',
            "((owner IS NOT NULL) AND (n > 0))",
            id="raw-boolean-composition",
        ),
    ],
)
def test_the_catalog_round_trip_is_not_drift(declared: str, live: str):
    """These pairs are the same predicate written two ways: the left is what
    ferro (or the author) wrote, the right is what Postgres stored."""
    assert declared != live
    assert _normalize_row_policy_expr(declared) == _normalize_row_policy_expr(live)


def test_a_real_body_change_is_drift():
    assert _normalize_row_policy_expr(CATALOG_SHORTHAND) != _normalize_row_policy_expr(
        _shorthand_expr(OTHER_SETTING)
    )


def test_parentheses_that_change_grouping_are_not_erased():
    assert _normalize_row_policy_expr("(a OR b) AND c") != _normalize_row_policy_expr(
        "a OR (b AND c)"
    )


# ---------------------------------------------------------------------------
# Render level
# ---------------------------------------------------------------------------


def test_an_unchanged_shorthand_policy_is_not_rebuilt():
    _define_ledger_row()
    statements, warnings = _render(_live())
    assert statements == []
    assert warnings == []


def test_a_drifted_shorthand_body_is_dropped_and_recreated():
    _define_ledger_row(setting=OTHER_SETTING)
    statements, warnings = _render(_live())
    assert statements == [DROP_POLICY_SQL, _create_policy_sql(OTHER_SETTING)]
    assert warnings == []


def test_command_drift_rebuilds():
    _define_ledger_row(command="select")
    statements, _ = _render(_live())
    assert statements == [DROP_POLICY_SQL, _create_policy_sql(command="SELECT")]


def test_restrictive_drift_rebuilds():
    _define_ledger_row(restrictive=True)
    statements, _ = _render(_live())
    assert statements == [DROP_POLICY_SQL, _create_policy_sql(restrictive=True)]


def test_a_clause_the_declaration_no_longer_asks_for_is_drift():
    """``FOR SELECT`` takes no WITH CHECK. A live policy that still carries one
    is drift ferro decides exactly — no body comparison needed."""
    _define_ledger_row(command="select")
    statements, _ = _render(_live(command="select", with_check=CATALOG_SHORTHAND))
    assert statements == [DROP_POLICY_SQL, _create_policy_sql(command="SELECT")]


def test_a_second_pass_over_the_rebuilt_policy_plans_nothing():
    """No flap: after the rebuild the catalog holds the new setting key, and
    reconciliation must go quiet."""
    _define_ledger_row(setting=OTHER_SETTING)
    statements, _ = _render(
        _live(using=CATALOG_SHORTHAND_OTHER, with_check=CATALOG_SHORTHAND_OTHER)
    )
    assert statements == []


def test_a_raw_body_difference_is_reported_and_never_rebuilt():
    """``BETWEEN`` is the stated limit: Postgres stores it as two comparisons,
    which no text normalizer can undo. Rebuilding on that difference would
    re-drift on the very next connect, so ferro reports it instead."""
    _define_raw_ledger_row('"id" BETWEEN 1 AND 5')
    statements, warnings = _render(
        _live(command="select", using="((id >= 1) AND (id <= 5))", with_check=None)
    )
    assert statements == []
    assert len(warnings) == 1
    assert POLICY_NAME in warnings[0]
    assert "does NOT rebuild" in warnings[0]
    # Both texts are printed: the author is the only one who can tell an edit
    # from a re-spelling.
    assert "BETWEEN 1 AND 5" in warnings[0]
    assert "(id >= 1)" in warnings[0]


def test_an_unchanged_raw_body_is_silent():
    _define_raw_ledger_row(
        '"id" IN (SELECT doc_id FROM membership WHERE member = '
        "NULLIF(current_setting('pinch.member', true), ''))"
    )
    statements, warnings = _render(
        _live(
            command="select",
            using="(id IN ( SELECT membership.doc_id\n   FROM membership\n  WHERE "
            "(membership.member = NULLIF(current_setting('pinch.member'::text, "
            "true), ''::text))))",
            with_check=None,
        )
    )
    assert statements == []
    assert warnings == []


def test_command_drift_on_a_raw_policy_still_rebuilds():
    """Metadata is exact for both forms — only the *body* of a raw policy is
    ferro's to report rather than rewrite."""
    _define_raw_ledger_row('"id" > 0')
    statements, _ = _render(_live(command="all", using="(id > 0)", with_check=None))
    assert statements == [
        DROP_POLICY_SQL,
        f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR SELECT USING ("id" > 0)',
    ]


def test_a_user_owned_policy_is_never_rebuilt():
    """A live policy ferro does not own shares no name with a declared one, so
    it can never be mistaken for drift — it is reported and left alone."""
    _define_ledger_row(setting=OTHER_SETTING)
    live = _live()
    live["policies"].append(
        {
            "name": "handwritten_admin",
            "command": "all",
            "restrictive": False,
            "using": "(true)",
            "with_check": None,
            "ferro_owned": False,
        }
    )
    statements, warnings = _render(live)
    assert not any("handwritten_admin" in sql for sql in statements)
    assert any("handwritten_admin" in w for w in warnings)


# ---------------------------------------------------------------------------
# Cross-emitter parity (AGENTS.md § I-1)
# ---------------------------------------------------------------------------


def test_row_policy_rebuild_statement_parity_pin():
    _define_ledger_row(setting=OTHER_SETTING)
    live = _live()
    plan = json.loads(
        _plan_row_security_reconcile(json.dumps(_model_ir()), json.dumps(live))
    )
    assert plan["drifted"] == [POLICY_NAME]
    assert plan["statements"] == [DROP_POLICY_SQL, _create_policy_sql(OTHER_SETTING)]

    runtime, _ = _render(live)
    assert plan["statements"] == runtime


# ---------------------------------------------------------------------------
# Live behavior
# ---------------------------------------------------------------------------


async def _pg_policies(table: str) -> list[dict]:
    return await fetch_all(
        "SELECT policyname, permissive, cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = $1 ORDER BY policyname",
        table,
    )


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_migrate_updates_rebuilds_a_drifted_policy(db_url):
    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")
    _rewind_registry()

    _define_ledger_row(setting=OTHER_SETTING)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        policies = await _pg_policies("ledgerrow")
        assert [policy["policyname"] for policy in policies] == [POLICY_NAME]
        assert "pinch.tenant_id" in policies[0]["qual"]
        assert "pinch.ledger_id" not in policies[0]["qual"]
        # Metadata only: the rebuild never touched the row.
        rows = await fetch_all("SELECT label FROM ledgerrow")
        assert [row["label"] for row in rows] == ["a1"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_rebuilt_policy_does_not_re_drift_on_the_next_connect(db_url):
    """The failure mode this whole comparison exists to prevent: rebuild, then
    reconcile again and see nothing."""
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(setting=OTHER_SETTING)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        live = await _live_row_security_for_test("ledgerrow")
        plan = json.loads(
            _plan_row_security_reconcile(json.dumps(_model_ir()), json.dumps(live))
        )
        assert plan["statements"] == []
        assert plan["drifted"] == []
        assert plan["warnings"] == []

    reset_engine()
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        live_again = await _live_row_security_for_test("ledgerrow")
        assert live_again == live


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_command_drift_rebuilds_against_a_live_table(db_url):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(command="select", restrictive=True)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        policies = await _pg_policies("ledgerrow")
        assert policies[0]["cmd"] == "SELECT"
        assert policies[0]["permissive"] == "RESTRICTIVE"
        assert policies[0]["with_check"] is None


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_rebuilt_policy_filters_by_the_new_setting(db_url):
    """The rebuild is not just catalog text — the new key is what scopes rows."""
    from ferro import transaction

    role = f"ferro_rls_{uuid.uuid4().hex[:12]}"
    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")
        await LedgerRow.create(ledger_id=LEDGER_B, label="b1")
    _rewind_registry()

    LedgerRow = _define_ledger_row(setting=OTHER_SETTING)
    await connect(db_url, migrate_updates=True)
    async with engines.session():
        schema = (await fetch_all("SELECT current_schema() AS s"))[0]["s"]
        try:
            await execute(f'CREATE ROLE "{role}" NOSUPERUSER')
            await execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
            await execute(f'GRANT SELECT ON "ledgerrow" TO "{role}"')
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{role}"')
                # The old key no longer scopes anything.
                await tx.execute(
                    "SELECT set_config($1, $2, true)", SETTING, str(LEDGER_A)
                )
                assert await tx.fetch_all("SELECT label FROM ledgerrow") == []
            async with transaction() as tx:
                await tx.execute(f'SET LOCAL ROLE "{role}"')
                await tx.execute(
                    "SELECT set_config($1, $2, true)", OTHER_SETTING, str(LEDGER_B)
                )
                rows = await tx.fetch_all("SELECT label FROM ledgerrow")
                assert [row["label"] for row in rows] == ["b1"]
        finally:
            await execute(f'DROP OWNED BY "{role}"')
            await execute(f'DROP ROLE IF EXISTS "{role}"')
