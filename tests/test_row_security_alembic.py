"""Alembic autogenerate for row security (#414, PRD #406).

The checks family is the template end to end: a custom autogenerate
operation consumes the SAME FFI decision the runtime reconciliation pass
executes (AGENTS.md § I-1 entries 15/16) and renders byte-identical
statements, so a reviewed-migration user gets exactly what ``auto_migrate``
would have done.

Two things ADR-0019 makes different from the checks family, on purpose:

* Removed declarations and orphaned ``rls_*`` policies are proposed for
  DROP by autogenerate with **no** destructive gate — the same posture
  ``_plan_check_drop`` takes (ADR-0013): the ``migrate_destructive`` flag is
  connect-time safety, a generated revision is reviewed before it runs.
* A **foreign** policy and an **unverifiable** raw-body drift never become
  an op at all, silently: the checks family has no comment-op precedent for
  either shape (see ``src/ferro/migrations/alembic.py``'s comparator
  docstring), so autogenerate says nothing and the runtime's own
  connect-time warnings remain the only word on them.
"""

import json
import uuid
from typing import ClassVar

import pytest
import sqlalchemy as sa

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


# ---------------------------------------------------------------------------
# Live autogenerate plumbing (mirrors tests/test_table_check_reconcile.py's
# "Alembic autogenerate" sections)
# ---------------------------------------------------------------------------


def _sync_url(postgres_base_url: str) -> str:
    for scheme in ("postgresql://", "postgres://"):
        if postgres_base_url.startswith(scheme):
            return "postgresql+psycopg://" + postgres_base_url[len(scheme) :]
    return postgres_base_url


def _produce_migration_script(postgres_base_url: str, db_schema_name: str):
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    metadata = get_metadata()
    engine = sa.create_engine(_sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            ctx = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            return produce_migrations(ctx, metadata)
    finally:
        engine.dispose()


def _autogen_upgrade_code(postgres_base_url: str, db_schema_name: str) -> str:
    from alembic.autogenerate import render_python_code

    script = _produce_migration_script(postgres_base_url, db_schema_name)
    return render_python_code(script.upgrade_ops)


def _autogen_upgrade_and_downgrade_code(
    postgres_base_url: str, db_schema_name: str
) -> tuple[str, str]:
    from alembic.autogenerate import render_python_code

    script = _produce_migration_script(postgres_base_url, db_schema_name)
    return (
        render_python_code(script.upgrade_ops),
        render_python_code(script.downgrade_ops),
    )


def _run_generated_code(code: str, postgres_base_url: str, db_schema_name: str) -> None:
    """Execute one side (upgrade or downgrade) of a generated revision's code
    against the live database, exactly as a real revision module's own
    ``upgrade()``/``downgrade()`` would — no temp file needed since
    ``render_python_code`` returns bare, already-runnable statements bound to
    a plain ``Operations`` instance (the checks/enum family's ops only ever
    render ``op.execute``/``op.drop_constraint`` calls, both real
    ``Operations`` methods)."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # `render_python_code` returns its lines pre-indented for splicing into a
    # revision module's `def upgrade():` body (or `downgrade`'s) — exactly
    # the shape the script.py.mako template expects. Reproduce that shape
    # literally instead of dedenting: a wrapper function, then call it.
    module = f"def _ferro_generated():\n{code}\n"
    engine = sa.create_engine(_sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            op = Operations(MigrationContext.configure(conn))
            namespace: dict = {"op": op, "sa": sa}
            exec(compile(module, "<generated-revision>", "exec"), namespace)
            namespace["_ferro_generated"]()
            conn.commit()
    finally:
        engine.dispose()


def _assert_statement_in_code(statement: str, code: str) -> None:
    """``render_python_code`` embeds every statement as a Python string
    literal (``op.execute('...')``); comparing against ``repr(statement)``
    (rather than the raw SQL) is what makes this survive statements that
    themselves contain single-quoted SQL literals, e.g. the shorthand's
    ``current_setting('pinch.ledger_id', true)``."""
    assert repr(statement) in code, (statement, code)


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


def test_sqlite_comparator_emits_nothing():
    from alembic.autogenerate import comparators
    from alembic.autogenerate.api import AutogenContext
    from alembic.migration import MigrationContext
    from alembic.operations.ops import UpgradeOps
    from sqlalchemy.dialects.sqlite.base import SQLiteDialect

    _define_ledger_row()
    from ferro.migrations import get_metadata

    metadata = get_metadata()
    migration_context = MigrationContext.configure(dialect=SQLiteDialect())
    autogen_context = AutogenContext(migration_context, metadata=metadata)
    upgrade_ops = UpgradeOps(ops=[])

    comparators.dispatch("schema")(autogen_context, upgrade_ops, [None])
    assert upgrade_ops.ops == []


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


def test_teardown_flag_labels_names_a_flag_only_tail():
    from ferro.migrations.alembic import _teardown_flag_labels

    assert _teardown_flag_labels([NO_FORCE_SQL]) == ["force"]
    assert _teardown_flag_labels([DISABLE_SQL]) == ["enabled"]
    assert _teardown_flag_labels([NO_FORCE_SQL, DISABLE_SQL]) == ["force", "enabled"]
    assert _teardown_flag_labels([DROP_POLICY_SQL]) == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_force_only_flip_proposes_a_narrow_drop_op_labeled_force(
    db_url, postgres_base_url, db_schema_name
):
    """A live-declared model whose declaration still exists but no longer
    asks for ``force=True`` proposes just ``NO FORCE ROW LEVEL SECURITY`` —
    no orphaned policy, no removed declaration — and the diff tuple names
    the flag instead of carrying an empty tuple next to real DDL."""
    _define_ledger_row(force=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_ledger_row(force=False)
    await connect(db_url, migrate_updates=True)  # warns; never clears FORCE itself

    from ferro.migrations.alembic import FerroRowSecurityDropOp

    script = _produce_migration_script(postgres_base_url, db_schema_name)
    drop_ops = [
        op for op in script.upgrade_ops.ops if isinstance(op, FerroRowSecurityDropOp)
    ]
    assert len(drop_ops) == 1, script.upgrade_ops.ops
    op = drop_ops[0]
    assert op.statements == [NO_FORCE_SQL]
    assert op.names == ["force"]
    # And its reverse is the same deliberate no-op as any other
    # FerroRowSecurityDropOp: restoring FORCE is a reviewed edit.
    assert op.reverse().statements == []


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
    reverts (they are gone again) — the exact `add`-op reverse
    ``FerroRowSecurityOp.reverse()`` computes via
    ``_teardown_row_security_statements``."""
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
    the flags; downgrade is an intentional no-op (ADR-0019 — recreating a
    dropped policy's live body is a reviewed edit, mirroring
    ``FerroCheckDropOp``), so the catalog stays torn down either way."""
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
        assert flags["relrowsecurity"] is False
        assert await _pg_policy_names("ledgerrow") == []
