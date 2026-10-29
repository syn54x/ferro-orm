"""Generated columns through the Alembic bridge.

```python
class Gc795Ledger(Model):
    amount_minor: int
    direction: int = Field(
        generated=lambda ledger: fn.sign(ledger.amount_minor), index=True
    )
```

The core decides each op's rendering. A new table or an added column on
Postgres is Alembic's own op, its column built with `sa.Computed(<the core's
rendering>, persisted=True)`; a new SQLite table is the pass's `CREATE TABLE`
as written, because SQLAlchemy cannot write SQLite's range guard. A changed
expression and a column made plain are always `op.execute` of the pass's
statement. Autogenerate against an auto-migrated database is empty.
"""

import json

import pytest

from ferro import Field, Model, _core, connect, ensure_resolved_modelset, fn
from tests._alembic_harness import (
    assert_statement_in_code,
    autogenerate,
    run_revision,
)
from tests.test_alembic_translator import (  # noqa: F401 - fixture
    _drift,
    _rewind_registry,
    cleanup_registry,
)

pytestmark = [pytest.mark.backend_matrix, pytest.mark.asyncio]


def _without_direction() -> None:
    class Gc795Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int


def _with_direction() -> None:
    class Gc795Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        direction: int = Field(
            generated=lambda ledger: fn.sign(ledger.amount_minor), index=True
        )


def _with_abs_direction() -> None:
    class Gc795Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        direction: int = Field(
            generated=lambda ledger: fn.abs(ledger.amount_minor), index=True
        )


def _with_plain_direction() -> None:
    class Gc795Ledger(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        direction: int = Field(index=True)


async def _auto_migrated(db_url: str, declare) -> None:
    """``declare``'s tables built by ``connect(auto_migrate=True)``, with one
    row, and the registry rewound."""
    declare()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()


def _empty(db_url, postgres_base_url, db_schema_name) -> None:
    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op." not in upgrade, upgrade
    assert "op." not in downgrade, downgrade


async def test_autogenerate_against_an_auto_migrated_database_is_empty(
    db_url, postgres_base_url, db_schema_name
):
    await _auto_migrated(db_url, _with_direction)
    _with_direction()

    _empty(db_url, postgres_base_url, db_schema_name)


async def test_a_new_model_with_a_generated_column_is_created_generated(
    db_url, postgres_base_url, db_schema_name
):
    _with_direction()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)

    if db_url.startswith("sqlite"):
        # The pass's CREATE TABLE, its range guard included.
        assert "op.create_table" not in upgrade, upgrade
        assert "GENERATED ALWAYS AS" in upgrade, upgrade
        assert "_ferro_range_direction" in upgrade, upgrade
    else:
        assert "op.create_table('gc795ledger'" in upgrade, upgrade
        assert (
            "sa.Computed('CAST(sign(CAST(\"amount_minor\" AS bigint)) AS bigint)', persisted=True)"
            in upgrade
        ), upgrade
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    _empty(db_url, postgres_base_url, db_schema_name)

    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    assert await _drift(db_url) == []


@pytest.mark.postgres_only
async def test_an_added_generated_column_is_alembics_op_with_sa_computed(
    db_url, postgres_base_url, db_schema_name
):
    await _auto_migrated(db_url, _without_direction)
    _with_direction()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)

    assert "op.add_column('gc795ledger'" in upgrade, upgrade
    assert "sa.Computed(" in upgrade, upgrade
    assert "# ferro: data-dependent" not in upgrade, upgrade
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []

    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _without_direction()
    assert await _drift(db_url) == []


@pytest.mark.postgres_only
async def test_a_changed_expression_is_the_passs_statement(
    db_url, postgres_base_url, db_schema_name
):
    await _auto_migrated(db_url, _with_direction)
    _with_abs_direction()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)

    assert_statement_in_code(
        'ALTER TABLE "gc795ledger" ALTER COLUMN "direction" SET EXPRESSION AS '
        '(abs(CAST("amount_minor" AS bigint)))',
        upgrade,
    )
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []

    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _with_direction()
    assert await _drift(db_url) == []


@pytest.mark.postgres_only
async def test_a_column_made_plain_is_the_passs_drop_expression(
    db_url, postgres_base_url, db_schema_name
):
    await _auto_migrated(db_url, _with_direction)
    _with_plain_direction()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)

    assert_statement_in_code(
        'ALTER TABLE "gc795ledger" ALTER COLUMN "direction" DROP EXPRESSION', upgrade
    )
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []

    # The down makes it generated again: a drop and an add of the column.
    assert "op.drop_column('gc795ledger', 'direction')" in downgrade, downgrade
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _with_direction()
    assert await _drift(db_url) == []


async def test_the_core_renders_the_expression_sa_computed_writes():
    _with_direction()
    [model] = [
        m
        for m in ensure_resolved_modelset()["payload"]["models"]
        if m["table_name"] == "gc795ledger"
    ]
    assert (
        _core._render_generated_expression(json.dumps(model), "direction", "postgres")
        == 'CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)'
    )
    assert (
        _core._render_generated_expression(json.dumps(model), "direction", "sqlite")
        == 'sign("amount_minor")'
    )
    with pytest.raises(ValueError, match="not a generated column"):
        _core._render_generated_expression(json.dumps(model), "amount_minor", "sqlite")
