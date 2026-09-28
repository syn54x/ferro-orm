"""Enum type drop on downgrade (#438; AGENTS.md § I-1 item 17).

A generated revision's ``create_table`` creates each named enum type as a
SQLAlchemy side effect of the column's ``sa.Enum`` — there is no Alembic op
for it. The rendered ``downgrade()`` is a bare ``op.drop_table`` that carries
no column objects, so nothing ever emits ``DROP TYPE``: the types survive
``alembic downgrade`` and the next upgrade fails with ``DuplicateObject``.

Ferro's comparator turns the type creation into a real op. It renders
nothing on upgrade (SQLAlchemy already emits ``CREATE TYPE`` inline) and
``DROP TYPE`` on downgrade, placed after the last ``drop_table``. The
decision (which types this revision's table creations bring into being) and
the rendered statement come from the Rust core over FFI
(``_plan_enum_type_drop``).
"""

from __future__ import annotations

from enum import StrEnum

import pytest
import sqlalchemy as sa

from ferro import Field, Model, clear_registry, connect, reset_engine
from ferro.raw import execute
from ferro.session import engines
from tests.test_row_security_alembic import (
    _assert_statement_in_code,
    _autogen_upgrade_and_downgrade_code,
    _run_generated_code,
    _sync_url,
)

DROP_COLOR_SQL = 'DROP TYPE "categorycolor"'
DROP_SIZE_SQL = 'DROP TYPE "cardsize"'


def _rewind_registry() -> None:
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.fixture(autouse=True)
def cleanup_registry():
    _rewind_registry()
    yield
    _rewind_registry()


class CategoryColor(StrEnum):
    RUST = "rust"
    AMBER = "amber"


class CardSize(StrEnum):
    SMALL = "small"
    LARGE = "large"


def _define_category() -> type[Model]:
    class Category(Model):
        id: int | None = Field(default=None, primary_key=True)
        color: CategoryColor | None = None

    return Category


def _define_card(*, with_color: bool = True) -> type[Model]:
    if with_color:

        class Card(Model):
            id: int | None = Field(default=None, primary_key=True)
            color: CategoryColor | None = None
            size: CardSize | None = None

        return Card

    class Card(Model):  # type: ignore[no-redef]
        id: int | None = Field(default=None, primary_key=True)
        size: CardSize | None = None

    return Card


def _live_enum_types(postgres_base_url: str, schema: str) -> list[str]:
    engine = sa.create_engine(_sync_url(postgres_base_url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE t.typtype = 'e' AND n.nspname = :schema "
                    "ORDER BY t.typname"
                ),
                {"schema": schema},
            )
            return [row[0] for row in rows]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The reported bug: create_table's types survive the downgrade
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_downgrade_drops_the_enum_types_this_revision_creates(
    db_url, postgres_base_url, db_schema_name
):
    """The issue's exact loop: autogenerate against an empty schema, run the
    upgrade, run the downgrade, and the type is gone — so the same upgrade
    runs again without ``DuplicateObject``."""
    _define_category()
    await connect(db_url)  # register only; the schema stays empty

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table('category'" in upgrade_code, upgrade_code
    assert DROP_COLOR_SQL not in upgrade_code, upgrade_code
    _assert_statement_in_code(DROP_COLOR_SQL, downgrade_code)
    assert downgrade_code.index("op.drop_table('category')") < downgrade_code.index(
        repr(DROP_COLOR_SQL)
    )
    assert "import ferro" not in downgrade_code, downgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_shared_by_two_new_tables_drops_once_after_both_tables(
    db_url, postgres_base_url, db_schema_name
):
    """Two models sharing one ``StrEnum`` are one type: one ``DROP TYPE``,
    after the last ``drop_table``; a second type on one of them drops too."""
    _define_category()
    _define_card()
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert downgrade_code.count(repr(DROP_COLOR_SQL)) == 1, downgrade_code
    _assert_statement_in_code(DROP_SIZE_SQL, downgrade_code)
    last_drop_table = max(
        downgrade_code.index("op.drop_table('category')"),
        downgrade_code.index("op.drop_table('card')"),
    )
    assert last_drop_table < downgrade_code.index(repr(DROP_SIZE_SQL))
    assert last_drop_table < downgrade_code.index(repr(DROP_COLOR_SQL))

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []


# ---------------------------------------------------------------------------
# Not this revision's to drop
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_a_surviving_table_still_uses_is_kept(
    db_url, postgres_base_url, db_schema_name
):
    """``category`` already lives (an earlier revision or auto-migrate created
    it); this revision adds ``card`` sharing ``categorycolor`` plus its own
    ``cardsize``. The downgrade drops ``cardsize`` only — ``categorycolor``
    still has a column on a table this downgrade leaves standing.

    The generated *upgrade* is not executed here: SQLAlchemy's
    ``create_table`` re-issues ``CREATE TYPE`` for the already-live
    ``categorycolor`` and Postgres refuses (#443; the upstream limitation
    reviewers hand-edit with ``create_type=False``). The live state the
    downgrade runs against comes from auto-migrate instead; once #443 lands
    this test should execute the upgrade it renders."""
    _define_category()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_category()
    _define_card()
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table('card'" in upgrade_code, upgrade_code
    assert "op.create_table('category'" not in upgrade_code, upgrade_code
    _assert_statement_in_code(DROP_SIZE_SQL, downgrade_code)
    assert repr(DROP_COLOR_SQL) not in downgrade_code, downgrade_code

    reset_engine()
    await connect(db_url, auto_migrate=True)  # bring `card` and `cardsize` live
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_that_already_exists_live_is_not_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """The downgrade reverses exactly what the upgrade creates. A type that
    is already live when the revision is generated is not created by this
    revision's ``create_table`` (SQLAlchemy would refuse to recreate it), so
    the downgrade must not claim it either."""
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"categorycolor\" AS ENUM ('rust', 'amber')")
    _rewind_registry()

    _define_category()
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table('category'" in upgrade_code, upgrade_code
    assert repr(DROP_COLOR_SQL) not in downgrade_code, downgrade_code


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_revision_that_creates_no_table_proposes_no_type_drop(
    db_url, postgres_base_url, db_schema_name
):
    """Models in sync generate nothing — no phantom op, no ``DROP TYPE``."""
    _define_category()
    await connect(db_url, auto_migrate=True)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "pass" in upgrade_code, upgrade_code
    assert "pass" in downgrade_code, downgrade_code
    assert "DROP TYPE" not in downgrade_code, downgrade_code


# ---------------------------------------------------------------------------
# SQLite: enums store as text; there is no type to drop
# ---------------------------------------------------------------------------


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_autogenerate_renders_no_type_drop(db_url, tmp_path):
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata

    _define_category()
    await connect(db_url)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'autogen.db'}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            script = produce_migrations(ctx, get_metadata())
        downgrade_code = render_python_code(script.downgrade_ops)
    finally:
        engine.dispose()

    assert "op.drop_table('category')" in downgrade_code, downgrade_code
    assert "DROP TYPE" not in downgrade_code, downgrade_code
