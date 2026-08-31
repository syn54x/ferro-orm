"""Leftover and foreign row policies (#413, ADR-0013's destructive ladder).

Two populations live on a policed table and ferro treats them very differently.

An **orphan** is a policy ferro owns — its name starts with ``rls_`` — that the
model no longer declares. It is still filtering rows, so ``migrate_updates``
names it and leaves it alone; only ``migrate_destructive`` drops it. That is the
same ladder leftover CHECKs use, applied more strictly on purpose: a leftover
CHECK rejects rows the model now allows, while a leftover policy may be the only
thing standing between two tenants.

A **foreign** policy is one ferro does not own — anything not named ``rls_*``.
Ferro never alters or drops one, on any flag, ever. It says the policy is there,
because a table's real behavior is every policy on it, not just the declared
ones.
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
from ferro.raw import execute, fetch_all

LEDGER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
LEDGER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")

SETTING = "pinch.ledger_id"
POLICY_NAME = "rls_ledgerrow_ledger_id"
ORPHAN_NAME = "rls_ledgerrow_retired"
FOREIGN_NAME = "handwritten_admin"

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


def _define_ledger_row(*, retired: bool = False) -> type[Model]:
    policies = [RowPolicy(column="ledger_id", setting=SETTING)]
    if retired:
        policies.append(
            RowPolicy(name="retired", command="select", using='"label" IS NOT NULL')
        )

    class LedgerRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        label: str

        __ferro_rls__: ClassVar = RowSecurity(*policies)

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


def _declared_live_policy() -> dict:
    return {
        "name": POLICY_NAME,
        "command": "all",
        "restrictive": False,
        "using": CATALOG_SHORTHAND,
        "with_check": CATALOG_SHORTHAND,
        "ferro_owned": True,
    }


def _orphan_policy() -> dict:
    return {
        "name": ORPHAN_NAME,
        "command": "select",
        "restrictive": False,
        "using": "(label IS NOT NULL)",
        "with_check": None,
        "ferro_owned": True,
    }


def _foreign_policy() -> dict:
    return {
        "name": FOREIGN_NAME,
        "command": "all",
        "restrictive": False,
        "using": "(true)",
        "with_check": None,
        "ferro_owned": False,
    }


def _live(*policies: dict) -> dict:
    return {"enabled": True, "forced": True, "policies": list(policies)}


def _render(
    live_row_security: dict, *, destructive: bool = False
) -> tuple[list[str], list[str]]:
    return _render_migration_sql_for_test(
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


def _model_ir() -> dict:
    return next(
        model
        for model in compile_registry_schema_ir()["payload"]["models"]
        if model["table_name"] == "ledgerrow"
    )


# ---------------------------------------------------------------------------
# Render level
# ---------------------------------------------------------------------------


def test_an_orphan_warns_on_updates_and_is_not_dropped():
    _define_ledger_row()
    statements, warnings = _render(_live(_declared_live_policy(), _orphan_policy()))
    assert statements == []
    assert len(warnings) == 1
    assert ORPHAN_NAME in warnings[0]
    assert "still filtering rows" in warnings[0]
    assert "migrate_destructive" in warnings[0]


def test_an_orphan_is_dropped_only_under_migrate_destructive():
    _define_ledger_row()
    statements, warnings = _render(
        _live(_declared_live_policy(), _orphan_policy()), destructive=True
    )
    assert statements == [f'DROP POLICY "{ORPHAN_NAME}" ON "ledgerrow"']
    # The run that removes protection says what it removed.
    assert len(warnings) == 1
    assert "tore down row security" in warnings[0]
    assert ORPHAN_NAME in warnings[0]


def test_a_policy_still_declared_is_never_an_orphan():
    _define_ledger_row(retired=True)
    statements, warnings = _render(
        _live(_declared_live_policy(), _orphan_policy()), destructive=True
    )
    assert statements == []
    assert warnings == []


def test_a_foreign_policy_is_reported_and_never_touched():
    _define_ledger_row()
    for destructive in (False, True):
        statements, warnings = _render(
            _live(_declared_live_policy(), _foreign_policy()), destructive=destructive
        )
        assert statements == [], destructive
        assert len(warnings) == 1, destructive
        assert FOREIGN_NAME in warnings[0]
        assert "does not own" in warnings[0]


def test_orphans_and_foreign_policies_are_told_apart():
    _define_ledger_row()
    plan = json.loads(
        _plan_row_security_reconcile(
            json.dumps(_model_ir()),
            json.dumps(
                _live(_declared_live_policy(), _orphan_policy(), _foreign_policy())
            ),
            "postgres",
            True,
        )
    )
    assert plan["extra"] == [ORPHAN_NAME]
    assert plan["foreign"] == [FOREIGN_NAME]
    assert plan["statements"] == [f'DROP POLICY "{ORPHAN_NAME}" ON "ledgerrow"']


def test_row_policy_drop_statement_parity_pin():
    _define_ledger_row()
    live = _live(_declared_live_policy(), _orphan_policy())
    plan = json.loads(
        _plan_row_security_reconcile(
            json.dumps(_model_ir()), json.dumps(live), "postgres", True
        )
    )
    runtime, _ = _render(live, destructive=True)
    assert plan["statements"] == runtime


# ---------------------------------------------------------------------------
# Live behavior
# ---------------------------------------------------------------------------


async def _pg_policy_names(table: str) -> list[str]:
    rows = await fetch_all(
        "SELECT policyname FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = $1 ORDER BY policyname",
        table,
    )
    return [row["policyname"] for row in rows]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_an_orphan_survives_updates_and_drops_on_destructive(db_url, recwarn):
    _define_ledger_row(retired=True)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME, ORPHAN_NAME]
    _rewind_registry()

    _define_ledger_row()
    recwarn.clear()
    await connect(db_url, migrate_updates=True)
    orphaned = [w for w in recwarn if ORPHAN_NAME in str(w.message)]
    assert len(orphaned) == 1, [str(w.message) for w in recwarn]
    async with engines.session():
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME, ORPHAN_NAME]

    reset_engine()
    recwarn.clear()
    await connect(db_url, migrate_destructive=True)
    async with engines.session():
        assert await _pg_policy_names("ledgerrow") == [POLICY_NAME]
    torn_down = [w for w in recwarn if "tore down row security" in str(w.message)]
    assert len(torn_down) == 1, [str(w.message) for w in recwarn]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_foreign_policy_survives_migrate_destructive(db_url, recwarn):
    LedgerRow = _define_ledger_row()
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(
            f'CREATE POLICY "{FOREIGN_NAME}" ON "ledgerrow" FOR ALL USING (true)'
        )
        await LedgerRow.create(ledger_id=LEDGER_A, label="a1")

    reset_engine()
    recwarn.clear()
    await connect(db_url, migrate_destructive=True)
    async with engines.session():
        assert await _pg_policy_names("ledgerrow") == [FOREIGN_NAME, POLICY_NAME]
    foreign = [w for w in recwarn if FOREIGN_NAME in str(w.message)]
    assert len(foreign) == 1, [str(w.message) for w in recwarn]
    assert "does not own" in str(foreign[0].message)
