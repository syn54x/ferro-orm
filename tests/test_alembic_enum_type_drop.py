"""Enum type provenance in a generated revision (#438, #443, #439; AGENTS.md
§ I-1 item 17; ADR-0020, ADR-0021, ADR-0022).

A generated revision's ``create_table`` creates each named enum type as a
SQLAlchemy side effect of the column's ``sa.Enum`` — there is no Alembic op
for it, and SQLAlchemy issues the ``CREATE TYPE`` unconditionally. Two
symptoms follow. The rendered ``downgrade()`` is a bare ``op.drop_table``
that carries no column objects, so nothing ever emits ``DROP TYPE`` and the
types survive ``alembic downgrade`` (#438). And a ``create_table`` whose enum
column reuses a type an earlier revision (or auto-migrate) already created
re-issues ``CREATE TYPE`` and the upgrade fails with ``DuplicateObject``
(#443). And an ``add_column`` is the one place SQLAlchemy never creates a
type: a revision whose only use of a new type is an ``add_column`` fails
with ``UndefinedObject`` (#439).

One decision answers all three: which types the revision *introduces*
(every column declaring the type is one the revision adds, a created
table's column or an ``add_column``), and for those, whether a
``create_table`` creates the type inline or the revision must execute the
``CREATE TYPE`` itself. An introduced type is created inline by
``create_table`` as before — or, when every column of it is an
``add_column``, by ferro's ``op.execute`` of the Rust-rendered guarded
``CREATE TYPE`` ahead of the table ops — and dropped by ferro's ``DROP TYPE``
op on downgrade, after the last ``drop_table``. A type the revision merely
*reuses* renders its ``create_table`` columns as
``postgresql.ENUM(..., create_type=False)`` and is never dropped. The
decision and both rendered statements come from the Rust core over FFI
(``_plan_enum_type_provenance``); the ``create_type=False`` rendering goes
through the bridge's ``render_item`` hook, wired in ``env.py``, because
SQLAlchemy's own ``repr`` of a ``postgresql.ENUM`` omits the flag. It is
decided from the revision alone, never from the live catalog (ADR-0020).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pytest
import sqlalchemy as sa

from ferro import Field, Model, clear_registry, connect, reset_engine
from ferro.raw import execute
from ferro.session import engines
from tests._alembic_harness import (
    assert_statement_in_code as _assert_statement_in_code,
    autogen_upgrade_and_downgrade_code as _autogen_upgrade_and_downgrade_code,
    produce_migration_script,
    run_generated_code as _run_generated_code,
    sync_url as _sync_url,
)

DROP_COLOR_SQL = 'DROP TYPE "categorycolor"'
# The guarded CREATE TYPE the auto-migrate create pass executes for the same
# model — pinned as a literal here and in ferro-ddl-lowering's unit tests
# (``render_pg_enum_create_type``), so the two migration doors cannot drift.
CREATE_COLOR_SQL = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t "
    "JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE t.typname = 'categorycolor' AND n.nspname = current_schema()) THEN "
    "CREATE TYPE \"categorycolor\" AS ENUM ('rust', 'amber'); "
    "END IF; END $$"
)
DROP_SIZE_SQL = 'DROP TYPE "cardsize"'
INTRODUCED_COLOR_TYPE = "sa.Enum('rust', 'amber', name='categorycolor')"
INTRODUCED_SIZE_TYPE = "sa.Enum('small', 'large', name='cardsize')"
REUSED_COLOR_TYPE = (
    "postgresql.ENUM('rust', 'amber', name='categorycolor', create_type=False)"
)
RENDER_ITEM_HINT = "render_item=render_item"


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


def _define_titled_card(*, with_color: bool) -> type[Model]:
    """A ``card`` with no enum column, or the same table plus ``color``."""
    if with_color:

        class Card(Model):
            id: int | None = Field(default=None, primary_key=True)
            title: str | None = None
            color: CategoryColor | None = None

        return Card

    class Card(Model):  # type: ignore[no-redef]
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None

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


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_shared_by_a_new_table_and_an_added_column_is_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """``card`` already lives without the enum column. This revision creates
    ``category(color)`` and adds ``color`` to ``card``: one new type, used by
    a created table and by a column this same revision adds. The downgrade
    drops that column and that table, so nothing uses the type afterwards —
    it must drop too, or the next upgrade fails with ``DuplicateObject``
    (the #438 symptom in its mixed shape)."""
    _define_titled_card(with_color=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_titled_card(with_color=True)
    _define_category()
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table('category'" in upgrade_code, upgrade_code
    assert "op.add_column('card'" in upgrade_code, upgrade_code
    # `create_table` creates the type inline, ahead of the add_column; a
    # rendered CREATE TYPE on top would fail with DuplicateObject (#439).
    assert repr(CREATE_COLOR_SQL) not in upgrade_code, upgrade_code
    assert upgrade_code.index("op.create_table('category'") < upgrade_code.index(
        "op.add_column('card'"
    )
    _assert_statement_in_code(DROP_COLOR_SQL, downgrade_code)
    drop_type_at = downgrade_code.index(repr(DROP_COLOR_SQL))
    assert downgrade_code.index("op.drop_table('category')") < drop_type_at
    assert downgrade_code.index("op.drop_column('card'") < drop_type_at

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


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
    still has a column on a table this downgrade leaves standing:
    ``category.color`` is declared and not one this revision adds, so the
    type is not the revision's (ADR-0020).

    The upgrade side agrees (#443, ADR-0021): ``card.color`` renders as
    ``postgresql.ENUM(..., create_type=False)`` so ``create_table`` does not
    re-issue ``CREATE TYPE`` for the live type, while ``card.size`` keeps
    the plain ``sa.Enum`` that creates ``cardsize`` inline. The issue's exact
    loop: the generated upgrade runs, the downgrade drops only ``cardsize``,
    and the upgrade runs again."""
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
    assert REUSED_COLOR_TYPE in upgrade_code, upgrade_code
    assert INTRODUCED_SIZE_TYPE in upgrade_code, upgrade_code
    assert INTRODUCED_COLOR_TYPE not in upgrade_code, upgrade_code
    _assert_statement_in_code(DROP_SIZE_SQL, downgrade_code)
    assert repr(DROP_COLOR_SQL) not in downgrade_code, downgrade_code
    assert "create_type" not in downgrade_code, downgrade_code
    assert "CREATE TYPE" not in upgrade_code, upgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_reused_type_needs_the_render_hook_wired_or_autogenerate_refuses(
    db_url, postgres_base_url, db_schema_name
):
    """SQLAlchemy's ``repr`` of a ``postgresql.ENUM`` drops ``create_type``,
    so the only way ``create_type=False`` reaches the revision file is
    Alembic's ``render_item`` hook, which lives in the project's ``env.py``.
    A revision that needs it, generated in an ``env.py`` that has not wired
    ``ferro.migrations.render_item``, fails at autogenerate with the one
    line to add — never with a file whose upgrade fails later (I-6)."""
    _define_category()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_category()
    _define_card()
    await connect(db_url)

    with pytest.raises(RuntimeError, match=RENDER_ITEM_HINT) as excinfo:
        _autogen_upgrade_and_downgrade_code(
            postgres_base_url, db_schema_name, extra_opts={"render_item": None}
        )
    assert "categorycolor" in str(excinfo.value)
    assert "card" in str(excinfo.value)

    def renders_nothing(type_, obj, autogen_context):
        return False

    with pytest.raises(RuntimeError, match=RENDER_ITEM_HINT):
        _autogen_upgrade_and_downgrade_code(
            postgres_base_url,
            db_schema_name,
            extra_opts={"render_item": renders_nothing},
        )


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_project_render_hook_that_delegates_to_ferro_is_enough(
    db_url, postgres_base_url, db_schema_name
):
    """A project with its own ``render_item`` composes ferro's the documented
    way — call it first, fall through on ``False`` — and the wiring check
    passes on behaviour, not identity."""
    from ferro.migrations import render_item

    _define_category()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_category()
    _define_card()
    await connect(db_url)

    seen: list[str] = []

    def project_render_item(type_, obj, autogen_context):
        rendered = render_item(type_, obj, autogen_context)
        if rendered is not False:
            return rendered
        seen.append(type_)
        return False

    upgrade_code, _downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url,
        db_schema_name,
        extra_opts={"render_item": project_render_item},
    )
    assert REUSED_COLOR_TYPE in upgrade_code, upgrade_code
    assert "type" in seen  # the project's hook still saw the other items
    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_shared_with_a_table_include_object_hides_is_kept(
    db_url, postgres_base_url, db_schema_name
):
    """``category`` lives and declares ``categorycolor``; this revision
    creates ``card``, which declares it too, while the project's
    ``include_object`` leaves ``category`` out of the revision entirely.

    ``category.color`` is still a declared column the revision does not add,
    so the type is not the revision's: hiding a table from the revision does
    not hide its columns from the decision. The upgrade reuses the type
    (``create_type=False``) and runs; the downgrade drops ``card`` and
    leaves ``categorycolor`` for ``category``."""
    _define_category()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_category()
    _define_card()
    await connect(db_url)

    def include_object(obj, name, type_, reflected, compare_to):
        return not (type_ == "table" and name == "category")

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url,
        db_schema_name,
        extra_opts={"include_object": include_object},
    )
    assert "op.create_table('card'" in upgrade_code, upgrade_code
    assert "'category'" not in upgrade_code, upgrade_code
    assert REUSED_COLOR_TYPE in upgrade_code, upgrade_code
    assert repr(DROP_COLOR_SQL) not in downgrade_code, downgrade_code
    _assert_statement_in_code(DROP_SIZE_SQL, downgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_on_a_column_the_downgrade_restores_is_kept(
    db_url, postgres_base_url, db_schema_name
):
    """Moving the column: ``card.color`` lives; this revision removes it and
    creates ``category(color)``. The only *declared* column of
    ``categorycolor`` is one the revision adds, but the downgrade puts
    ``card.color`` back before any type drop could run, so that restored
    column still uses the type. The revision's own ``drop_column`` is what
    tells the decision so; no ``DROP TYPE`` is rendered, and the upgrade's
    ``category.color`` reuses the type instead of re-creating it (#443) —
    the whole loop runs."""
    _define_titled_card(with_color=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_titled_card(with_color=False)
    _define_category()
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table('category'" in upgrade_code, upgrade_code
    assert "op.drop_column('card', 'color')" in upgrade_code, upgrade_code
    assert REUSED_COLOR_TYPE in upgrade_code, upgrade_code
    assert "op.add_column('card'" in downgrade_code, downgrade_code
    assert repr(DROP_COLOR_SQL) not in downgrade_code, downgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]
    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_pre_existing_orphan_type_is_dropped_with_the_table_that_adopts_it(
    db_url, postgres_base_url, db_schema_name
):
    """The decision is made from the revision alone (ADR-0020). Here the dev
    database already holds an orphaned ``categorycolor`` (the residue an
    older broken downgrade leaves) and this revision creates ``category``,
    the only declared user. The rendered downgrade drops the type anyway:
    on every database the revision can run against, its ``create_table`` is
    what created the type, and a file that omitted the drop because of one
    developer's local catalog would be incomplete everywhere else.

    The upgrade is not executed: the type is the revision's by provenance,
    so its column keeps the inline-creating ``sa.Enum`` and, on this one
    database, collides with the orphan (ADR-0020's stated consequence — the
    orphan is removed by hand; #443 covers a type a *declared surviving
    column* uses, not a leftover nothing declares)."""
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
    assert INTRODUCED_COLOR_TYPE in upgrade_code, upgrade_code
    _assert_statement_in_code(DROP_COLOR_SQL, downgrade_code)
    assert downgrade_code.index("op.drop_table('category')") < downgrade_code.index(
        repr(DROP_COLOR_SQL)
    )


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_introduced_by_add_column_alone_is_created_and_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """No ``create_table`` at all: the existing ``card`` gains ``color``. The
    only column of ``categorycolor`` is one this revision adds, so the type
    is the revision's — and nothing creates it: SQLAlchemy creates a named
    enum type inline with ``create_table`` only, so Alembic's ``add_column``
    ran against a type that did not exist and failed with
    ``UndefinedObject`` (#439). The generated ``upgrade()`` now executes the
    Rust-rendered guarded ``CREATE TYPE`` ahead of the ``add_column`` — the
    statement the auto-migrate create pass executes for the same model —
    and the ``downgrade()`` drops the type after ``drop_column`` as before.
    The issue's loop: upgrade, downgrade, upgrade again."""
    _define_titled_card(with_color=False)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    _define_titled_card(with_color=True)
    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table(" not in upgrade_code, upgrade_code
    assert "op.add_column('card'" in upgrade_code, upgrade_code
    _assert_statement_in_code(CREATE_COLOR_SQL, upgrade_code)
    assert upgrade_code.index(repr(CREATE_COLOR_SQL)) < upgrade_code.index(
        "op.add_column('card'"
    )
    assert upgrade_code.count("CREATE TYPE") == 1, upgrade_code
    assert "import ferro" not in upgrade_code, upgrade_code
    _assert_statement_in_code(DROP_COLOR_SQL, downgrade_code)
    assert downgrade_code.index("op.drop_column('card'") < downgrade_code.index(
        repr(DROP_COLOR_SQL)
    )
    assert "CREATE TYPE" not in downgrade_code, downgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]

    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_two_added_columns_of_one_new_type_create_it_once(
    db_url, postgres_base_url, db_schema_name
):
    """``card`` and ``category`` both live without their enum column; this
    revision adds ``color`` to each. One type, two ``add_column``s, no
    ``create_table``: the type is created once, ahead of both, and dropped
    once after both on downgrade."""

    class Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None

    class Category(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Card(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None
        color: CategoryColor | None = None

    class Category(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None
        color: CategoryColor | None = None

    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "op.create_table(" not in upgrade_code, upgrade_code
    assert "op.add_column('card'" in upgrade_code, upgrade_code
    assert "op.add_column('category'" in upgrade_code, upgrade_code
    assert upgrade_code.count(repr(CREATE_COLOR_SQL)) == 1, upgrade_code
    create_at = upgrade_code.index(repr(CREATE_COLOR_SQL))
    assert create_at < upgrade_code.index("op.add_column('card'")
    assert create_at < upgrade_code.index("op.add_column('category'")
    assert downgrade_code.count(repr(DROP_COLOR_SQL)) == 1, downgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["categorycolor"]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []
    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_revision_that_creates_no_table_proposes_no_type_drop(
    db_url, postgres_base_url, db_schema_name
):
    """Models in sync generate nothing — no phantom op, no ``DROP TYPE``."""
    _define_category()
    await connect(db_url, auto_migrate=True)

    script = produce_migration_script(postgres_base_url, db_schema_name)
    assert script.upgrade_ops.is_empty(), script.upgrade_ops.ops
    assert script.downgrade_ops.is_empty(), script.downgrade_ops.ops


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_mixed_case_type_name_round_trips_quoted(
    db_url, postgres_base_url, db_schema_name
):
    """A ``Literal`` field with an explicit mixed-case ``enum_type_name``
    creates ``"CategoryColor"`` (SQLAlchemy quotes it); the rendered drop
    is always-quoted too, so it names the same object end to end."""

    class Swatch(Model):
        id: int | None = Field(default=None, primary_key=True)
        color: Literal["rust", "amber"] | None = Field(
            default=None, json_schema_extra={"enum_type_name": "CategoryColor"}
        )

    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    assert "name='CategoryColor'" in upgrade_code, upgrade_code
    _assert_statement_in_code('DROP TYPE "CategoryColor"', downgrade_code)

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == ["CategoryColor"]
    _run_generated_code(downgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_type_created_by_statement_lands_before_tables_beside_label_additions(
    db_url, postgres_base_url, db_schema_name
):
    """The two before-tables enum families share the front of the revision:
    ``cardsize`` lives with a stale label set and gains ``large`` (label
    addition, ADR-0011) while ``card`` gains ``color`` of the brand-new
    ``categorycolor`` (type creation, ADR-0022). Both land ahead of the
    ``add_column``, and the upgrade runs."""
    from ferro.raw import execute
    from ferro.session import engines

    class Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute("CREATE TYPE \"cardsize\" AS ENUM ('small')")
        await execute('ALTER TABLE "card" ADD COLUMN "size" "cardsize"')
    _rewind_registry()

    class Card(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        title: str | None = None
        size: CardSize | None = None
        color: CategoryColor | None = None

    await connect(db_url)

    upgrade_code, downgrade_code = _autogen_upgrade_and_downgrade_code(
        postgres_base_url, db_schema_name
    )
    add_column_at = upgrade_code.index("op.add_column('card'")
    assert upgrade_code.index(repr(CREATE_COLOR_SQL)) < add_column_at, upgrade_code
    # The renderer repr-quotes the statement; assert on its stable substrings.
    add_value_at = upgrade_code.index("ADD VALUE IF NOT EXISTS")
    assert add_value_at < add_column_at, upgrade_code
    assert "large" in upgrade_code[add_value_at:add_column_at], upgrade_code
    assert "autocommit_block" in upgrade_code, upgrade_code
    # `cardsize` survives on a column the revision never added; `categorycolor`
    # is the revision's and is dropped after the column.
    _assert_statement_in_code(DROP_COLOR_SQL, downgrade_code)
    assert 'DROP TYPE "cardsize"' not in downgrade_code, downgrade_code

    _run_generated_code(upgrade_code, postgres_base_url, db_schema_name)
    assert _live_enum_types(postgres_base_url, db_schema_name) == [
        "cardsize",
        "categorycolor",
    ]


# ---------------------------------------------------------------------------
# SQLite: enums store as text; there is no type to drop
# ---------------------------------------------------------------------------


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_sqlite_autogenerate_renders_no_type_drop(db_url, tmp_path):
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata, render_item

    _define_category()
    await connect(db_url)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'autogen.db'}")
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"render_item": render_item})
            script = produce_migrations(ctx, get_metadata())
        upgrade_code = render_python_code(script.upgrade_ops, render_item=render_item)
        downgrade_code = render_python_code(
            script.downgrade_ops, render_item=render_item
        )
    finally:
        engine.dispose()

    assert "op.drop_table('category')" in downgrade_code, downgrade_code
    assert "DROP TYPE" not in downgrade_code, downgrade_code
    assert "CREATE TYPE" not in upgrade_code, upgrade_code
    assert "create_type" not in upgrade_code, upgrade_code
    assert INTRODUCED_COLOR_TYPE in upgrade_code, upgrade_code


# ---------------------------------------------------------------------------
# ``render_item``: the one rendering SQLAlchemy's repr cannot do
# ---------------------------------------------------------------------------


def _postgres_autogen_context():
    from alembic.autogenerate.api import AutogenContext
    from alembic.migration import MigrationContext

    return AutogenContext(MigrationContext.configure(dialect_name="postgresql"))


def test_render_item_renders_only_a_non_creating_postgres_enum():
    """``repr(postgresql.ENUM(..., create_type=False))`` omits the flag, so
    Alembic's default rendering would put a type-creating column in the
    file. Ferro's hook renders exactly that case, with the dialect import
    the revision needs, and declines everything else so Alembic's own
    renderers (and a project's own hook) keep their say."""
    from sqlalchemy.dialects import postgresql

    from ferro.migrations import render_item

    ctx = _postgres_autogen_context()
    reused = postgresql.ENUM("rust", "amber", name="categorycolor", create_type=False)
    assert render_item("type", reused, ctx) == REUSED_COLOR_TYPE
    assert "from sqlalchemy.dialects import postgresql" in ctx.imports

    creating = postgresql.ENUM("rust", "amber", name="categorycolor")
    assert render_item("type", creating, ctx) is False
    plain = sa.Enum("rust", "amber", name="categorycolor")
    assert render_item("type", plain, ctx) is False
    assert render_item("type", sa.String(), ctx) is False
    assert render_item("column", sa.Column("color", reused), ctx) is False
    assert render_item("server_default", None, ctx) is False


def test_render_item_quotes_labels_and_names_like_the_revision_needs():
    """Labels and the type name are Python literals in the file: an
    apostrophe in a label must survive the round trip."""
    from sqlalchemy.dialects import postgresql

    from ferro.migrations import render_item

    ctx = _postgres_autogen_context()
    odd = postgresql.ENUM("it's", "plain", name="od'd", create_type=False)
    rendered = render_item("type", odd, ctx)
    expected = "postgresql.ENUM(\"it's\", 'plain', name=\"od'd\", create_type=False)"
    assert rendered == expected
    rebuilt = eval(rendered, {"postgresql": postgresql})  # noqa: S307 - test-only
    assert list(rebuilt.enums) == ["it's", "plain"]
    assert rebuilt.name == "od'd"
    assert rebuilt.create_type is False
