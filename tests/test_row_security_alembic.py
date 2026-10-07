"""Alembic autogenerate for row security (#414, PRD #406).

Since ADR-0041 the bridge writes what the one planner decides: the row
security ops of a generated revision are the planner's, rendered as the
byte-identical statements the reconciliation pass executes (AGENTS.md § I-1
entries 15/16), so a reviewed-migration user gets exactly what
``auto_migrate`` would have done.

Two postures ADR-0019 sets, on purpose:

* Removed declarations and orphaned ``rls_*`` policies are proposed for
  DROP with **no** destructive gate: autogenerate plans with destructive
  changes on, since a generated revision is reviewed before it runs; the
  ``migrate_destructive`` flag is connect-time safety.
* A **foreign** policy and an **unverifiable** raw-body drift never become
  an op at all: the planner reports them in ``always_warnings`` and plans
  nothing, so autogenerate says nothing and the runtime's own connect-time
  warnings remain the only word on them.
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
from ferro._core import _plan_row_security_reconcile, _render_migration_sql_for_test
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.raw import execute, fetch_all, fetch_one
from tests._alembic_harness import (
    assert_statement_in_code as _assert_statement_in_code,
    autogen_upgrade_and_downgrade_code as _autogen_upgrade_and_downgrade_code,
    autogen_upgrade_code as _autogen_upgrade_code,
    run_generated_code as _run_generated_code,
)

LEDGER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")

SETTING = "pinch.ledger_id"
POLICY_NAME = "rls_ledgerrow_ledger_id"
ORPHAN_NAME = "rls_ledgerrow_retired"
FOREIGN_NAME = "handwritten_admin"

SHORTHAND_EXPR = (
    "\"ledger_id\" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid"
)
CREATE_POLICY_SQL = (
    f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR ALL '
    f"USING ({SHORTHAND_EXPR}) WITH CHECK ({SHORTHAND_EXPR})"
)
ENABLE_SQL = 'ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY'
FORCE_SQL = 'ALTER TABLE "ledgerrow" FORCE ROW LEVEL SECURITY'
DROP_POLICY_SQL = f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"'
NO_FORCE_SQL = 'ALTER TABLE "ledgerrow" NO FORCE ROW LEVEL SECURITY'
DISABLE_SQL = 'ALTER TABLE "ledgerrow" DISABLE ROW LEVEL SECURITY'

#: Real ``pg_get_expr(polqual, polrelid)`` output for the policy ferro renders
#: on a ``uuid`` column (see tests/test_row_security_reconcile.py).
CATALOG_SHORTHAND = (
    "(ledger_id = (NULLIF(current_setting('pinch.ledger_id'::text, true), "
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
    *, declared: bool = True, force: bool = True, retired: bool = False
) -> type[Model]:
    if not declared:

        class LedgerRow(Model):
            id: int | None = Field(default=None, primary_key=True)
            ledger_id: uuid.UUID
            label: str

        return LedgerRow

    policies = [RowPolicy(column="ledger_id", setting=SETTING)]
    if retired:
        policies.append(
            RowPolicy(name="retired", command="select", using='"label" IS NOT NULL')
        )

    class LedgerRow(Model):  # type: ignore[no-redef]
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(*policies, force=force)

    return LedgerRow


def _define_ledger_row_raw(*, using: str) -> type[Model]:
    """A raw ``using=``/``with_check=`` policy, for the unverifiable case."""

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(name="ledger_id", command="all", using=using, with_check=using)
        )

    return LedgerRow


def _model_ir() -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "ledgerrow"
    )


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


def _render(live_row_security: dict, *, destructive: bool = False) -> list[str]:
    statements, _ = _render_migration_sql_for_test(
        "ledgerrow",
        json.dumps(compile_registry_schema_ir()),
        json.dumps(LEDGER_LIVE_COLUMNS),
        "postgres",
        True,
        destructive,
        "",
        "",
        "",
        json.dumps(live_row_security),
    )
    return statements


async def _pg_flags(table: str) -> dict:
    row = await fetch_one(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        f"WHERE oid = '\"{table}\"'::regclass"
    )
    assert row is not None
    return row


async def _pg_policy_names(table: str) -> list[str]:
    rows = await fetch_all(
        "SELECT policyname FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = $1 ORDER BY policyname",
        table,
    )
    return [row["policyname"] for row in rows]


# ---------------------------------------------------------------------------
# New declaration on an existing table: flags + policy
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_new_declaration_on_a_live_table_proposes_flags_then_the_policy(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url)  # no auto-migrate: the database stays drifted

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    for statement in (ENABLE_SQL, FORCE_SQL, CREATE_POLICY_SQL):
        _assert_statement_in_code(statement, code)
    assert code.index(repr(ENABLE_SQL)) < code.index(repr(CREATE_POLICY_SQL))
    assert "import ferro" not in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_new_declaration_on_a_brand_new_table_lands_after_create_table(
    db_url, postgres_base_url, db_schema_name
):
    """No SA construct renders row security inline (metadata has no policy
    concept), so a table this SAME revision creates still needs its own op,
    off the create-pass decision (#418) — landing after ``create_table``."""
    _define_ledger_row()
    await connect(db_url)  # register the model; never create the table

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "op.create_table(" in code, code
    for statement in (ENABLE_SQL, FORCE_SQL, CREATE_POLICY_SQL):
        _assert_statement_in_code(statement, code)
    assert code.index("op.create_table(") < code.index(repr(ENABLE_SQL))


# ---------------------------------------------------------------------------
# Drift: shorthand body, and command/restrictive/roles metadata
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_shorthand_body_drift_proposes_a_rebuild(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        # Hand-edit the shorthand body so it no longer normalizes to what
        # ferro would render — the body-drift shape, not a metadata one.
        await execute(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"')
        await execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR ALL '
            "USING (true) WITH CHECK (true)"
        )

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    _assert_statement_in_code(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"', code)
    _assert_statement_in_code(CREATE_POLICY_SQL, code)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_command_drift_proposes_a_rebuild(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"')
        await execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" FOR SELECT '
            f"USING ({SHORTHAND_EXPR})"
        )

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    _assert_statement_in_code(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"', code)
    _assert_statement_in_code(CREATE_POLICY_SQL, code)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_restrictive_drift_proposes_a_rebuild(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"')
        await execute(
            f'CREATE POLICY "{POLICY_NAME}" ON "ledgerrow" AS RESTRICTIVE FOR ALL '
            f"USING ({SHORTHAND_EXPR}) WITH CHECK ({SHORTHAND_EXPR})"
        )

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    _assert_statement_in_code(f'DROP POLICY "{POLICY_NAME}" ON "ledgerrow"', code)
    _assert_statement_in_code(CREATE_POLICY_SQL, code)


# ---------------------------------------------------------------------------
# Removed declaration and orphans: drop/teardown proposed with no gate
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_removed_declaration_proposes_the_full_teardown(
    db_url, postgres_base_url, db_schema_name, recwarn
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(declared=False)
    await connect(db_url, migrate_updates=True)  # warns; never tears down itself

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    _assert_statement_in_code(DROP_POLICY_SQL, code)
    _assert_statement_in_code(NO_FORCE_SQL, code)
    _assert_statement_in_code(DISABLE_SQL, code)
    assert "import ferro" not in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_orphan_policy_proposes_a_drop(db_url, postgres_base_url, db_schema_name):
    _define_ledger_row(retired=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME, ORPHAN_NAME]
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url, migrate_updates=True)  # warns; never drops itself

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    _assert_statement_in_code(f'DROP POLICY "{ORPHAN_NAME}" ON "ledgerrow"', code)
    assert repr(DROP_POLICY_SQL) not in code, code  # the still-declared policy stays


# ---------------------------------------------------------------------------
# Silence: foreign policies and unverifiable raw-body drift
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_foreign_policy_is_never_proposed(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(
            f'CREATE POLICY "{FOREIGN_NAME}" ON "ledgerrow" FOR ALL USING (true)'
        )

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert FOREIGN_NAME not in code, code
    assert "ferro_row_security" not in code, code
    assert code.strip().splitlines()[-1].strip() == "# ### end Alembic commands ###"
    assert "pass" in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_unverifiable_raw_body_drift_is_silent(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row_raw(using='"label" IS NOT NULL')
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row_raw(using='"label" IS NULL')  # edited, indistinguishable
    await connect(db_url)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "POLICY" not in code.upper(), code
    assert "pass" in code, code


# ---------------------------------------------------------------------------
# The phantom-diff test: an unchanged declaration emits nothing
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_once_auto_migrate_applied_the_declaration(
    db_url, postgres_base_url, db_schema_name
):
    """No phantom diffs (AGENTS.md § I-1): what ``auto_migrate`` applied,
    autogenerate does not propose again — the two migration doors agree."""
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "POLICY" not in code.upper(), code
    assert "ROW LEVEL SECURITY" not in code.upper(), code
    assert "pass" in code, code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_is_empty_after_migrate_updates_reconciled_it(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url, migrate_updates=True)

    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    assert "POLICY" not in code.upper(), code
    assert "ROW LEVEL SECURITY" not in code.upper(), code
    assert "pass" in code, code


# ---------------------------------------------------------------------------
# Dialect gate (ADR-0014): SQLite proposes nothing
# ---------------------------------------------------------------------------


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_autogenerate_writes_no_row_security(db_url):
    """A new SQLite table declaring row security is created without it, as
    the create pass creates it (one warning at connect, no DDL)."""
    import sqlalchemy as sa
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata
    from tests._alembic_harness import autogen_opts

    _define_ledger_row()
    await connect(db_url)

    engine = sa.create_engine(f"sqlite:///{db_url.split(':', 1)[1].split('?')[0]}")
    try:
        with engine.connect() as conn:
            script = produce_migrations(
                MigrationContext.configure(conn, opts=autogen_opts()), get_metadata()
            )
    finally:
        engine.dispose()
    code = render_python_code(script.upgrade_ops)
    assert "op.create_table('ledgerrow'" in code, code
    assert "POLICY" not in code.upper(), code
    assert "ROW LEVEL SECURITY" not in code.upper(), code


# ---------------------------------------------------------------------------
# Byte-parity (AGENTS.md § I-1): the SAME statements as the runtime pass
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_proposes_the_same_add_as_the_runtime(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url)

    runtime_statements = _render({"enabled": False, "forced": False, "policies": []})
    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    for statement in runtime_statements:
        _assert_statement_in_code(statement, code)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_autogenerate_proposes_the_same_teardown_as_the_runtime(
    db_url, postgres_base_url, db_schema_name
):
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(declared=False)
    await connect(db_url, migrate_updates=True)

    runtime_statements = _render(
        {
            "enabled": True,
            "forced": True,
            "policies": [
                {
                    "name": POLICY_NAME,
                    "command": "all",
                    "restrictive": False,
                    "using": CATALOG_SHORTHAND,
                    "with_check": CATALOG_SHORTHAND,
                    "ferro_owned": True,
                }
            ],
        },
        destructive=True,
    )
    code = _autogen_upgrade_code(postgres_base_url, db_schema_name)
    for statement in runtime_statements:
        _assert_statement_in_code(statement, code)


def test_the_reconcile_seam_the_comparator_consumes_is_directly_pinned():
    """Guards the exact seam the comparator relies on, with every drift
    category present AT ONCE — a missing policy, a drifted one, an orphan,
    and a ``force`` flag not yet applied — so the non-destructive plan is
    genuinely non-empty (a fixture where it plans nothing would make the
    prefix assertion below trivially true regardless of whether the seam
    actually holds). Calling ``_plan_row_security_reconcile`` non-destructive
    then destructive for the SAME (model, live) pair must yield the
    destructive statements as the non-destructive statements plus a strict
    tail — the slicing the comparator performs to split add-ops from
    drop-ops without re-deriving anything. Mirrored at the Rust level by
    ``plan_row_security_reconcile_destructive_call_extends_the_non_destructive_one_as_a_strict_prefix``."""

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting=SETTING),  # drifted below
            RowPolicy(name="invitee", command="select", using='"label" IS NOT NULL'),
            force=True,  # live.forced is False below: a flag statement too
        )

    live = {
        "enabled": True,
        "forced": False,
        "policies": [
            {
                "name": POLICY_NAME,
                "command": "select",  # declared "all": metadata drift
                "restrictive": False,
                "using": CATALOG_SHORTHAND,
                "with_check": None,
                "ferro_owned": True,
            },
            {
                "name": ORPHAN_NAME,
                "command": "select",
                "restrictive": False,
                "using": "(label IS NOT NULL)",
                "with_check": None,
                "ferro_owned": True,
            },
        ],
    }
    non_destructive = json.loads(
        _plan_row_security_reconcile(
            json.dumps(_model_ir()), json.dumps(live), "postgres", False
        )
    )
    destructive = json.loads(
        _plan_row_security_reconcile(
            json.dumps(_model_ir()), json.dumps(live), "postgres", True
        )
    )
    # Every category actually fired, or this pin is not exercising what it
    # claims to.
    assert non_destructive["missing"] == ["rls_ledgerrow_invitee"]
    assert non_destructive["drifted"] == [POLICY_NAME]
    assert destructive["extra"] == [ORPHAN_NAME]
    assert non_destructive["statements"] != []

    prefix_len = len(non_destructive["statements"])
    assert destructive["statements"][:prefix_len] == non_destructive["statements"]
    tail = destructive["statements"][prefix_len:]
    assert tail == [f'DROP POLICY "{ORPHAN_NAME}" ON "ledgerrow"']


# ---------------------------------------------------------------------------
# The narrower force=False flip: no orphan, no removed declaration, just the
# FORCE flag clearing on its own (#414 review item 5)
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_force_only_flip_proposes_a_narrow_drop_op_labeled_force(
    db_url, postgres_base_url, db_schema_name
):
    """A live-declared model whose declaration still exists but no longer
    asks for ``force=True`` proposes just ``NO FORCE ROW LEVEL SECURITY`` —
    no orphaned policy, no removed declaration — and the downgrade, the
    planner run back to the live database, forces it again."""
    _define_ledger_row(force=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(force=False)
    await connect(db_url, migrate_updates=True)  # warns; never clears FORCE itself

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    _assert_statement_in_code(NO_FORCE_SQL, upgrade_code)
    assert "POLICY" not in upgrade_code.upper(), upgrade_code
    assert "DISABLE" not in upgrade_code, upgrade_code
    _assert_statement_in_code(FORCE_SQL, downgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        assert (await _pg_flags("ledgerrow"))["relforcerowsecurity"] is False
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        assert (await _pg_flags("ledgerrow"))["relforcerowsecurity"] is True


# ---------------------------------------------------------------------------
# Round trip: the generated revision applies and reverts (#414)
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_generated_revision_round_trips(
    db_url, postgres_base_url, db_schema_name
):
    """Upgrade applies (live catalog carries the flags/policy); downgrade
    reverts (they are gone again) — the planner run back to the live
    database, which held neither (ADR-0041)."""
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row()
    await connect(db_url)  # no auto-migrate: the database stays drifted

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    _assert_statement_in_code(CREATE_POLICY_SQL, upgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is True
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME]

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is False
        assert flags["relforcerowsecurity"] is False
        assert await _pg_policy_names("ledgerrow") == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_downgrade_never_disables_row_security_that_predates_the_declaration(
    db_url, postgres_base_url, db_schema_name
):
    """Regression for a review BLOCKER: a table whose row security predates
    ferro's declaration (a DBA enabled it and wrote a foreign policy) must
    downgrade to "policy dropped, flags untouched" — never
    ``DISABLE ROW LEVEL SECURITY`` on a fence ferro never turned on. That is
    exactly the incident
    ``ferro_ddl_lowering::excess_row_security_flag_statements``'s ownership
    gate exists to prevent, reached through the back door of an
    autogenerated downgrade instead of ``migrate_destructive`` if
    ``_synthetic_ferro_owned_live`` ever hardcoded ``enabled=True``."""
    _define_ledger_row(declared=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute('ALTER TABLE "ledgerrow" ENABLE ROW LEVEL SECURITY')
        await execute(
            f'CREATE POLICY "{FOREIGN_NAME}" ON "ledgerrow" FOR ALL USING (true)'
        )
    _rewind_registry()

    _define_ledger_row(force=False)
    await connect(db_url)  # no auto-migrate: only ferro's own policy is missing

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    # The flags are already on: upgrade must not propose either one.
    assert "ENABLE ROW LEVEL SECURITY'" not in upgrade_code
    assert "FORCE ROW LEVEL SECURITY'" not in upgrade_code
    _assert_statement_in_code(CREATE_POLICY_SQL, upgrade_code)
    # And the downgrade must be equally narrow: drop the policy, leave the
    # DBA's fence exactly as it was.
    assert "DISABLE" not in downgrade_code
    assert "NO FORCE" not in downgrade_code
    _assert_statement_in_code(DROP_POLICY_SQL, downgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is False
        assert await _pg_policy_names("ledgerrow") == [FOREIGN_NAME, POLICY_NAME]

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        # Still enabled: the DBA's fence is untouched by the downgrade.
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is False
        assert await _pg_policy_names("ledgerrow") == [FOREIGN_NAME]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_generated_teardown_revision_round_trips(
    db_url, postgres_base_url, db_schema_name
):
    """The removed-declaration teardown side: upgrade drops the policy and
    the flags; the downgrade is the planner run back to the live database
    (ADR-0041), so it turns the flags back on and recreates the policy from
    the body the catalog printed for it."""
    _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(declared=False)
    await connect(db_url, migrate_updates=True)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    _assert_statement_in_code(DROP_POLICY_SQL, upgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is False
        assert flags["relforcerowsecurity"] is False
        assert await _pg_policy_names("ledgerrow") == []

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    async with engines.session():
        flags = await _pg_flags("ledgerrow")
        assert flags["relrowsecurity"] is True
        assert flags["relforcerowsecurity"] is True
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME]
