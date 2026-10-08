"""The Alembic bridge as a translator of the one planner (#533, ADR-0041).

```python
# env.py
context.configure(connection=connection, target_metadata=get_metadata(), **ferro_options())
```

``alembic revision --autogenerate`` reads the live database the way the
reconciliation pass does, asks the one planner what turns it into the
models (destructive changes on), and writes the planner's ops: Alembic's own
op where it has one, ``op.execute`` of the pass's statement where it does
not. ``downgrade()`` is the planner run back to the live database. These
tests pin the round trip (upgrade, then no drift; downgrade, then the old
models see no drift), the renames, the live-only repairs, the markers, the
refusals, both ``env.py`` shapes, and ``get_metadata()`` over a ferro
configuration.
"""

import asyncio
import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Check,
    Field,
    ForeignKey,
    Model,
    Relation,
    RowPolicy,
    RowSecurity,
    _core,
    clear_registry,
    connect,
    ensure_resolved_modelset,
    reset_engine,
)
from ferro.base import FerroField
from ferro.raw import execute
from ferro.session import engines
from tests._alembic_harness import (
    assert_statement_in_code,
    autogen_opts,
    autogenerate,
    engine_for,
    run_revision,
)

DESTRUCTIVE = json.dumps({"destructive": True})


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


async def _drift(db_url: str) -> list[dict]:
    """What the one planner would still change to turn the live database
    into the registered models: the pass's question, asked on a private
    connection. ``[]`` is "no drift"."""
    envelope = ensure_resolved_modelset()
    name = f"tr533_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        live, facts = await _core._live_schema_ir(name, json.dumps(envelope))
        dialect = _core.connection_backend(name)
    finally:
        await _core._disconnect(name)
    assert dialect is not None
    plan = json.loads(
        _core._plan_from_ir(
            live, json.dumps(envelope), dialect, DESTRUCTIVE, True, facts
        )
    )
    return [op for op in plan["operations"] if op["statements"] or op["reports"]]


class Tr533Flavor(StrEnum):
    SWEET = "sweet"
    SALTY = "salty"


# ---------------------------------------------------------------------------
# The casebook round trip
# ---------------------------------------------------------------------------


def _card_v1() -> None:
    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str


def _card_v2() -> None:
    class Tr533Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        cards: Relation[list["Tr533Card"]] = BackRef()

    class Tr533Card(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("note_set", lambda card: card.note != None),  # noqa: E711
        )

        id: int | None = Field(default=None, primary_key=True)
        name: Annotated[str, FerroField(index=True)]
        note: str | None = None
        flavor: Tr533Flavor | None = None
        team: Annotated[
            Tr533Team | None, ForeignKey(related_name="cards", on_delete="CASCADE")
        ] = None


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_revision_runs_the_pass_and_its_downgrade_restores_the_live_schema(
    db_url, postgres_base_url, db_schema_name
):
    """Every op the planner plans for the casebook change — a new table, an
    enum type, plain, enum and foreign-key columns, an index, a table check —
    is in the revision; after ``upgrade()`` the models see no drift, after
    ``downgrade()`` the old models see none."""
    _card_v1()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _card_v2()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.create_table('tr533team'" in upgrade, upgrade
    assert "op.add_column('tr533card'" in upgrade, upgrade
    assert "op.create_index('idx_tr533card_name'" in upgrade, upgrade
    # The column is Alembic's op; the constraint the pass adds with it runs
    # as the pass writes it.
    assert_statement_in_code(
        'ALTER TABLE "tr533card" ADD CONSTRAINT "fk_tr533card_team_id_tr533team" '
        'FOREIGN KEY ("team_id") REFERENCES "tr533team" ("id") ON DELETE CASCADE',
        upgrade,
    )
    # A new column lands before the check that reads it (#423), by the
    # planner's own order.
    assert upgrade.index("'note'") < upgrade.index("ck_tr533card_note_set"), upgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []

    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _card_v1()
    assert await _drift(db_url) == []


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_sqlite_revision_of_native_changes_round_trips(
    db_url, postgres_base_url, db_schema_name
):
    """Changes SQLite alters natively still autogenerate: a new table, a
    nullable column and an index."""

    def v2() -> None:
        class Tr533Shelf(Model):
            id: int | None = Field(default=None, primary_key=True)

        class Tr533Card(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: Annotated[str, FerroField(index=True)]
            note: str | None = None

    _card_v1()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    v2()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.create_table('tr533shelf'" in upgrade, upgrade
    assert "op.add_column('tr533card'" in upgrade, upgrade
    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _card_v1()
    assert await _drift(db_url) == []


# ---------------------------------------------------------------------------
# The pin: a table auto_migrate built plans nothing, either way
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_an_auto_migrated_table_plans_nothing_forward_or_back(
    db_url, postgres_base_url, db_schema_name
):
    """A native enum, a table check, a ``db_check`` and a ``force=True``
    policy, built by ``auto_migrate``: the planner plans nothing from the
    live database to the models, the reverse plans nothing, and
    autogenerate writes an empty revision."""

    class Tr533Ledger(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("named", lambda ledger: ledger.name != None),  # noqa: E711
        )
        __ferro_rls__: ClassVar = RowSecurity(
            RowPolicy(column="ledger_id", setting="pinch.ledger_id"), force=True
        )

        id: int | None = Field(default=None, primary_key=True)
        ledger_id: uuid.UUID
        name: str
        flavor: Tr533Flavor
        mood: Annotated[Tr533Flavor, FerroField(db_type="text", db_check=True)] = (
            Tr533Flavor.SWEET
        )

    await connect(db_url, auto_migrate=True)
    envelope = json.dumps(ensure_resolved_modelset())
    name = f"tr533_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        live, facts = await _core._live_schema_ir(name, envelope)
    finally:
        await _core._disconnect(name)
    forward = json.loads(
        _core._plan_from_ir(live, envelope, "postgres", DESTRUCTIVE, True, facts)
    )
    reverse = json.loads(
        _core._plan_reverse_from_ir(live, envelope, "postgres", DESTRUCTIVE, facts)
    )
    assert forward["operations"] == []
    assert reverse["operations"] == []

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "pass" in upgrade and "op." not in upgrade, upgrade
    assert "op." not in downgrade, downgrade


# ---------------------------------------------------------------------------
# Rename hints
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_rename_hints_render_alembic_renames_and_the_derived_names(
    db_url, postgres_base_url, db_schema_name
):
    """A table and a column rename hint are ``op.rename_table`` and
    ``op.alter_column(new_column_name=…)``; the index name they drag is the
    pass's own ``ALTER INDEX … RENAME``; the downgrade renames them all
    back."""

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: Annotated[str, FerroField(index=True)]

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    def renamed() -> None:
        class Tr533Deck(Model):
            __ferro_renamed_from__: ClassVar[str] = "tr533card"

            id: int | None = Field(default=None, primary_key=True)
            title: Annotated[str, FerroField(index=True, renamed_from="label")]

    renamed()
    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.rename_table('tr533card', 'tr533deck')" in upgrade, upgrade
    flat = " ".join(upgrade.split())
    assert "op.alter_column('tr533deck', 'label', new_column_name='title')" in flat, (
        upgrade
    )
    assert_statement_in_code(
        'ALTER INDEX "idx_tr533card_label" RENAME TO "idx_tr533deck_title"', upgrade
    )
    assert "drop_table" not in upgrade and "create_table" not in upgrade, upgrade
    assert "op.rename_table('tr533deck', 'tr533card')" in downgrade, downgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()

    class Tr533Card(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        label: Annotated[str, FerroField(index=True)]

    assert await _drift(db_url) == []


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_leftover_check_renamed_with_its_table_comes_back_under_its_old_name(
    db_url, postgres_base_url, db_schema_name
):
    """ADR-0050's dropped-leftover case beside a table rename: the upgrade
    renames ``tr533card`` and the leftover check only the live facts carry,
    then drops the check; the downgrade renames the table back and restores
    the check under its old name, with the body the catalog printed."""

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute(
            'ALTER TABLE "tr533card" ADD CONSTRAINT "ck_tr533card_old" CHECK (id > 0)'
        )
    _rewind_registry()

    class Tr533Deck(Model):
        __ferro_renamed_from__: ClassVar[str] = "tr533card"

        id: int | None = Field(default=None, primary_key=True)

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.rename_table('tr533card', 'tr533deck')" in upgrade, upgrade
    assert_statement_in_code(
        'ALTER TABLE "tr533deck" RENAME CONSTRAINT "ck_tr533card_old" TO "ck_tr533deck_old"',
        upgrade,
    )
    assert_statement_in_code(
        'ALTER TABLE "tr533deck" DROP CONSTRAINT "ck_tr533deck_old"', upgrade
    )
    assert "op.rename_table('tr533deck', 'tr533card')" in downgrade, downgrade
    assert_statement_in_code(
        'ALTER TABLE "tr533card" ADD CONSTRAINT "ck_tr533card_old" CHECK ((id > 0))',
        downgrade,
    )

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    engine = engine_for(db_url, postgres_base_url)
    try:
        with engine.connect() as conn:
            names = conn.execute(
                sa.text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = :schema AND t.relname = 'tr533card' "
                    "AND c.contype = 'c'"
                ),
                {"schema": db_schema_name},
            ).scalars()
            assert list(names) == ["ck_tr533card_old"]
    finally:
        engine.dispose()


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_live_hints_old_table_is_read_by_the_hint_alone(
    db_url, postgres_base_url, db_schema_name
):
    """The project's ``include_object`` keeps the old table ``tr533card`` out
    of the tables a revision drops, so only the live read's hint rule
    (``tables_to_read``) brings it in: the revision renames it, never creates
    an empty ``tr533deck`` beside it."""

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        label: str

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Tr533Deck(Model):
        __ferro_renamed_from__: ClassVar[str] = "tr533card"

        id: int | None = Field(default=None, primary_key=True)
        label: str

    def project_filter(obj, name, type_, reflected, compare_to):
        return not (type_ == "table" and name == "tr533card")

    from ferro.migrations import ferro_options

    upgrade, downgrade = autogenerate(
        db_url,
        postgres_base_url,
        db_schema_name,
        extra_opts=ferro_options(include_object=project_filter),
    )
    assert "op.rename_table('tr533card', 'tr533deck')" in upgrade, upgrade
    assert "create_table" not in upgrade and "drop_table" not in upgrade, upgrade
    assert "op.rename_table('tr533deck', 'tr533card')" in downgrade, downgrade


# ---------------------------------------------------------------------------
# Live-only repairs: a NOT VALID check, an invalid index
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
# Alembic's inspector reflects the deliberately invalid index and warns.
@pytest.mark.filterwarnings("ignore:Can't validate argument 'dialect_options'")
async def test_a_not_valid_check_is_validated_and_an_invalid_index_rebuilt(
    db_url, postgres_base_url, db_schema_name
):
    class Tr533Card(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("named", lambda card: card.name != None),  # noqa: E711
        )

        id: int | None = Field(default=None, primary_key=True)
        name: Annotated[str, FerroField(index=True)]

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute('ALTER TABLE "tr533card" DROP CONSTRAINT "ck_tr533card_named"')
        await execute(
            'ALTER TABLE "tr533card" ADD CONSTRAINT "ck_tr533card_named" '
            "CHECK (name IS NOT NULL) NOT VALID"
        )
        await execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
            "'\"idx_tr533card_name\"'::regclass"
        )

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert_statement_in_code(
        'ALTER TABLE "tr533card" VALIDATE CONSTRAINT "ck_tr533card_named"', upgrade
    )
    assert_statement_in_code('DROP INDEX "idx_tr533card_name"', upgrade)
    assert_statement_in_code(
        'CREATE INDEX IF NOT EXISTS "idx_tr533card_name" ON "tr533card" ("name")',
        upgrade,
    )
    assert "op." not in downgrade, "a repair has nothing to undo"

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []


# ---------------------------------------------------------------------------
# Markers and the irreversible step
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_required_column_is_marked_data_dependent_and_a_drop_destructive(
    db_url, postgres_base_url, db_schema_name
):
    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        legacy: str | None = None

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Tr533Card(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int | None = Field(default=None, primary_key=True)
        name: str
        size: int

    upgrade, _ = autogenerate(db_url, postgres_base_url, db_schema_name)
    lines = upgrade.splitlines()
    add_at = next(
        i for i, line in enumerate(lines) if "op.add_column('tr533card'" in line
    )
    assert lines[add_at - 1].strip().startswith("# ferro: data-dependent"), upgrade
    assert "ferro migrate new" in lines[add_at - 1], upgrade
    drop_at = next(
        i for i, line in enumerate(lines) if "op.drop_column('tr533card'" in line
    )
    assert lines[drop_at - 1].strip().startswith("# ferro: destructive"), upgrade


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_label_addition_is_irreversible_in_the_downgrade(
    db_url, postgres_base_url, db_schema_name
):
    await connect(db_url)
    async with engines.session():
        await execute("CREATE TYPE \"tr533flavor\" AS ENUM ('sweet')")
        await execute(
            'CREATE TABLE "tr533card" ("id" serial PRIMARY KEY, '
            '"flavor" "tr533flavor" NOT NULL)'
        )
    reset_engine()

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        flavor: Tr533Flavor

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "ADD VALUE IF NOT EXISTS" in upgrade, upgrade
    assert "raise RuntimeError(" in downgrade, downgrade
    assert "append-only" in downgrade, downgrade


# ---------------------------------------------------------------------------
# A dropped required column: its downgrade demands values too
# ---------------------------------------------------------------------------


def _bra_author_with_nickname() -> None:
    class BraAuthor(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        nickname: str


def _bra_author() -> None:
    class BraAuthor(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_dropped_required_column_comes_back_marked_data_dependent(
    db_url, postgres_base_url, db_schema_name
):
    """Dropping ``nickname: str`` (``NOT NULL``, no default): putting it back
    asks the rows already there for a value no statement supplies, so the
    downgrade writes the plain ``add_column`` under ``# ferro:
    data-dependent``, exactly as an upgrade adding it would — not the
    planner's raw "Cannot add NOT NULL column" error. On an empty table it
    reaches the old models."""
    _bra_author_with_nickname()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _bra_author()

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.drop_column('braauthor', 'nickname')" in upgrade, upgrade
    lines = downgrade.splitlines()
    add_at = next(
        i for i, line in enumerate(lines) if "op.add_column('braauthor'" in line
    )
    assert lines[add_at - 1].strip() == (
        "# ferro: data-dependent (fails while braauthor has rows; ferro migrations "
        "generate the backfill: `ferro migrate new`)"
    ), downgrade
    assert "sa.Column('nickname', sa.String(), nullable=False)" in lines[add_at], (
        downgrade
    )
    assert "op.execute" not in downgrade, downgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _bra_author_with_nickname()
    assert await _drift(db_url) == []


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_dropped_required_column_is_irreversible_on_sqlite(
    db_url, postgres_base_url, db_schema_name
):
    """SQLite cannot add a ``NOT NULL`` column with no default even to an
    empty table: the downgrade says so and names ``ferro migrate new``."""
    _bra_author_with_nickname()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _bra_author()

    _, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "raise RuntimeError(" in downgrade, downgrade
    assert "braauthor.nickname" in downgrade, downgrade
    assert "`ferro migrate new`" in downgrade, downgrade
    assert "op.add_column" not in downgrade, downgrade


# ---------------------------------------------------------------------------
# A dropped model takes the enum type only it used
# ---------------------------------------------------------------------------


class BraOrderKind(StrEnum):
    POST = "post"
    COURIER = "courier"


def _bra_shop(*, with_order: bool) -> None:
    class BraCustomer(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    if with_order:

        class BraOrder(Model):
            id: int | None = Field(default=None, primary_key=True)
            kind: BraOrderKind = BraOrderKind.POST


DROP_KIND = 'DROP TYPE "braorderkind"'
_NO_MODELS = {
    "ir_kind": "schema",
    "ir_version": 1,
    "payload": {"dialect_agnostic": True, "models": []},
}


async def _enum_types(db_url: str) -> list[str]:
    name = f"bra_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        _, facts = await _core._live_schema_ir(name, json.dumps(_NO_MODELS))
    finally:
        await _core._disconnect(name)
    return sorted(json.loads(facts)["enum_labels"])


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_dropped_model_drops_its_enum_type_and_the_downgrade_recreates_it(
    db_url, postgres_base_url, db_schema_name
):
    """Deleting ``BraOrder``, whose ``kind`` is the native enum
    ``braorderkind`` no other model uses: the revision drops the table and
    then the type (the pass's ``DROP TYPE``), and the downgrade creates the
    type before the table that needs it. The round trip leaves no type
    behind and puts back the old schema."""
    _bra_shop(with_order=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _bra_shop(with_order=False)

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert upgrade.count("op.drop_table(") == 1, upgrade
    drop_table_at = upgrade.index("op.drop_table('braorder')")
    assert_statement_in_code(DROP_KIND, upgrade)
    assert drop_table_at < upgrade.index(repr(DROP_KIND)), upgrade
    create_type_at = downgrade.index("CREATE TYPE")
    assert "braorderkind" in downgrade[create_type_at:].splitlines()[0], downgrade
    assert create_type_at < downgrade.index("op.create_table('braorder'"), downgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    assert "braorderkind" not in await _enum_types(db_url)

    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    assert "braorderkind" in await _enum_types(db_url)
    _rewind_registry()
    _bra_shop(with_order=True)
    assert await _drift(db_url) == []


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_live_table_the_projects_filter_excludes_is_never_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """A live table no metadata declares is dropped only when Alembic's own
    comparison would drop it: one the project's ``include_object`` keeps out
    of autogenerate stays out of the revision."""
    _bra_shop(with_order=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute('CREATE TABLE "bra_foreign" ("id" integer PRIMARY KEY)')

    def project_filter(obj, name, type_, reflected, compare_to):
        return not (type_ == "table" and name == "bra_foreign")

    from ferro.migrations import ferro_options

    upgrade, downgrade = autogenerate(
        db_url,
        postgres_base_url,
        db_schema_name,
        extra_opts=ferro_options(include_object=project_filter),
    )
    assert "bra_foreign" not in upgrade + downgrade, upgrade


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_live_table_the_projects_name_filter_excludes_is_never_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """The same for the project's ``include_name``: a table it keeps out of
    reflection is never one ferro drops."""
    _bra_shop(with_order=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute('CREATE TABLE "bra_named" ("id" integer PRIMARY KEY)')

    def include_name(name, type_, parent_names):
        return not (type_ == "table" and name == "bra_named")

    upgrade, downgrade = autogenerate(
        db_url,
        postgres_base_url,
        db_schema_name,
        extra_opts={"include_name": include_name},
    )
    assert "bra_named" not in upgrade + downgrade, upgrade


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@pytest.mark.parametrize("version_table", [None, "bra_alembic_version"])
async def test_the_version_table_is_never_dropped(
    db_url, postgres_base_url, db_schema_name, version_table
):
    """A database Alembic manages carries its version table, which no
    metadata declares: deleting a model never drops it, under the default
    name or the project's own ``version_table``."""
    _bra_shop(with_order=False)
    await connect(db_url, auto_migrate=True)
    name = version_table or "alembic_version"
    async with engines.session():
        await execute(f'CREATE TABLE "{name}" ("version_num" varchar(32) NOT NULL)')

    upgrade, downgrade = autogenerate(
        db_url,
        postgres_base_url,
        db_schema_name,
        extra_opts={"version_table": version_table} if version_table else None,
    )
    assert name not in upgrade + downgrade, upgrade
    assert "drop_table" not in upgrade, upgrade


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_ferros_tracking_tables_are_never_dropped(
    db_url, postgres_base_url, db_schema_name
):
    """The tracking tables no model declares are not tables a deleted model
    left behind: asked directly (below the tracked-database refusal),
    ``_dropped_tables`` lists a deleted model's table and none of ferro's
    tracking tables."""
    from alembic.autogenerate.api import AutogenContext
    from alembic.migration import MigrationContext

    from ferro.migrations import alembic as bridge
    from ferro.migrations import get_metadata

    _bra_shop(with_order=True)
    await connect(db_url, auto_migrate=True)
    name = f"tr_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        # A locked run's first write creates the tracking tables.
        tracked = await _core._open_tracked(name, None, "migrations")
        async with tracked.locked(5.0) as run:
            await run.remove_baseline()
    finally:
        await _core._disconnect(name)
    _rewind_registry()
    _bra_shop(with_order=False)

    tracking = set(_core._tracking_table_names())
    engine = engine_for(db_url, postgres_base_url)
    try:
        with engine.connect() as conn:
            if db_schema_name is not None:
                conn.execute(sa.text(f'SET search_path TO "{db_schema_name}"'))
            live = set(sa.inspect(conn).get_table_names())
            assert tracking <= live, live
            # No ferro object filter: it also hides the tracking tables, so
            # only the finder's own skip is under test.
            opts = autogen_opts({"include_object": None})
            context = MigrationContext.configure(conn, opts=opts)
            autogen = AutogenContext(context, get_metadata(), opts=opts)
            dropped = bridge._dropped_tables(autogen)
    finally:
        engine.dispose()
    assert dropped == ["braorder"], dropped


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_autogenerate_without_ferro_options_is_refused_naming_the_line(
    db_url, postgres_base_url, db_schema_name
):
    _card_v1()
    await connect(db_url)
    with pytest.raises(RuntimeError, match=r"\*\*ferro_options\(\)"):
        autogenerate(
            db_url,
            postgres_base_url,
            db_schema_name,
            extra_opts={"include_object": None},
        )


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_refused_rename_hint_refuses_autogenerate(
    db_url, postgres_base_url, db_schema_name
):
    """A rename hint whose old name the models still declare is refused by
    the planner (ADR-0032); autogenerate refuses with its words rather than
    write a revision that ignores the hint."""
    _card_v1()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Tr533Deck(Model):
        __ferro_renamed_from__: ClassVar[str] = "tr533card"

        id: int | None = Field(default=None, primary_key=True)
        name: str

    with pytest.raises(RuntimeError) as refused:
        autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "rename hint refused" in str(refused.value)
    assert "tr533card" in str(refused.value)


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_primary_key_change_is_refused_with_the_recipe(
    db_url, postgres_base_url, db_schema_name
):
    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str

    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Tr533Card(Model):  # type: ignore[no-redef]  # noqa: F811
        id: int
        code: str = Field(primary_key=True)

    with pytest.raises(RuntimeError) as refused:
        autogenerate(db_url, postgres_base_url, db_schema_name)
    # The generator's refusal, word for word (`GenerateError::PrimaryKeyChange`).
    assert str(refused.value) == (
        'ferro: autogenerate refused: changing the primary key of "tr533card" is not '
        "generated: write it as a new table (ferro migrate new --data-step …), a "
        "backfill of parent and children, and a drop; see the Migrations docs § "
        "Changing a primary key"
    )


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_sqlite_change_needing_a_rebuild_is_refused_naming_ferro_migrate_new(
    db_url, postgres_base_url, db_schema_name
):
    _card_v1()
    await connect(db_url, auto_migrate=True)
    _rewind_registry()

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str | None = None  # nullability: a table rebuild on SQLite

    with pytest.raises(RuntimeError) as refused:
        autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "SQLite table rebuild" in str(refused.value)
    assert "tr533card.name" in str(refused.value)
    assert "`ferro migrate new`" in str(refused.value)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_tracked_database_is_refused_naming_ferro_migrate_new(
    db_url, postgres_base_url, db_schema_name, db_backend
):
    """A database ferro migrations track (it carries the tracking
    tables) takes model changes from ``ferro migrate new``."""
    _card_v1()
    await connect(db_url, auto_migrate=True)
    name = f"tr533_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        # A locked run's first write creates the tracking tables.
        tracked = await _core._open_tracked(name, None, "migrations")
        async with tracked.locked(5.0) as run:
            await run.remove_baseline()
    finally:
        await _core._disconnect(name)

    with pytest.raises(RuntimeError) as refused:
        autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "`ferro migrate new`" in str(refused.value)
    assert "keeps **ferro_options()" in str(refused.value)


# ---------------------------------------------------------------------------
# The two env.py shapes: no event loop, and inside one
# ---------------------------------------------------------------------------


def _autogenerate_sqlite_file(path: Path) -> str:
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext

    from ferro.migrations import get_metadata
    from tests._alembic_harness import autogen_opts

    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            script = produce_migrations(
                MigrationContext.configure(conn, opts=autogen_opts()), get_metadata()
            )
    finally:
        engine.dispose()
    return render_python_code(script.upgrade_ops)


def test_a_synchronous_env_py_reads_the_live_database(tmp_path):
    """A plain ``env.py`` runs autogenerate with no event loop: the bridge
    runs its read to completion on this thread."""
    path = tmp_path / "sync.db"
    _card_v1()
    asyncio.run(connect(f"sqlite:{path}?mode=rwc", auto_migrate=True))
    reset_engine()
    _rewind_registry()

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        note: str | None = None

    code = _autogenerate_sqlite_file(path)
    assert "op.add_column('tr533card', sa.Column('note'" in code, code
    assert "create_table" not in code, code


@pytest.mark.asyncio
async def test_an_async_env_py_reads_the_live_database(tmp_path):
    """An async ``env.py`` runs autogenerate inside ``connection.run_sync``,
    within a running event loop where ``asyncio.run`` cannot nest: the
    bridge reads on a thread with its own loop."""
    path = tmp_path / "async.db"
    _card_v1()
    await connect(f"sqlite:{path}?mode=rwc", auto_migrate=True)
    _rewind_registry()

    class Tr533Card(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        note: str | None = None

    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from ferro.migrations import get_metadata
    from tests._alembic_harness import autogen_opts

    def do_run_migrations(connection) -> str:
        """The synchronous half of Alembic's async ``env.py`` template."""
        script = produce_migrations(
            MigrationContext.configure(connection, opts=autogen_opts()), get_metadata()
        )
        return render_python_code(script.upgrade_ops)

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.connect() as connection:
            code = await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()
    assert "op.add_column('tr533card', sa.Column('note'" in code, code
    assert "create_table" not in code, code


# ---------------------------------------------------------------------------
# ferro_options(): the project's own hooks compose
# ---------------------------------------------------------------------------


def test_ferro_options_hides_ferro_tables_and_asks_the_projects_filter():
    from ferro.migrations import ferro_options

    seen: list[str | None] = []

    def project_filter(obj, name, type_, reflected, compare_to):
        seen.append(name)
        return name != "audit_log"

    options = ferro_options(include_object=project_filter)
    include = options["include_object"]
    include.hide(["tr533card"])
    assert include(None, "tr533card", "table", True, None) is False
    assert include(None, "_ferro_migrations", "table", True, None) is False
    assert include(None, "_ferro_migrations_format", "table", True, None) is False
    assert include(None, "audit_log", "table", True, None) is False
    assert include(None, "invoice", "table", True, None) is True
    assert seen == ["audit_log", "invoice"]
    assert set(options) == {"include_object", "render_item"}


# ---------------------------------------------------------------------------
# get_metadata() reads the project's configuration
# ---------------------------------------------------------------------------

MODELS = """
from typing import Annotated

from ferro import Model
from ferro.base import FerroField


class {name}(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
"""


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_imports: None
) -> Path:
    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _package(root: Path, package: str, model: str) -> None:
    (root / package).mkdir()
    (root / package / "__init__.py").write_text("")
    (root / package / "models.py").write_text(MODELS.format(name=model))


def test_get_metadata_imports_the_configured_databases_models(project: Path):
    from ferro.migrations import get_metadata

    package = f"tr533_one_{project.parent.name.replace('-', '_')}"
    _package(project, package, "Tr533Invoice")
    (project / "ferro.toml").write_text(
        f'models = ["{package}.models"]\ndialects = ["postgres"]\n'
    )
    metadata = get_metadata()
    assert sorted(metadata.tables) == ["tr533invoice"]


def test_get_metadata_names_one_of_several_databases(project: Path):
    from ferro.migrations import get_metadata

    suffix = project.parent.name.replace("-", "_")
    _package(project, f"tr533_app_{suffix}", "Tr533Order")
    _package(project, f"tr533_stats_{suffix}", "Tr533Metric")
    (project / "ferro.toml").write_text(
        f'[databases.app]\nmodels = ["tr533_app_{suffix}.models"]\n'
        f'dialects = ["postgres"]\ndirectory = "db/app"\n\n'
        f'[databases.stats]\nmodels = ["tr533_stats_{suffix}.models"]\n'
        f'dialects = ["postgres"]\ndirectory = "db/stats"\n'
    )
    assert sorted(get_metadata(database="stats").tables) == ["tr533metric"]
    assert sorted(get_metadata(database="app").tables) == ["tr533order"]


def test_get_metadata_without_a_configuration_renders_every_registered_model(
    project: Path,
):
    from ferro.migrations import get_metadata

    _card_v1()
    metadata = get_metadata()
    assert sorted(metadata.tables) == ["tr533card"]
    assert isinstance(metadata.tables["tr533card"], sa.Table)


def test_engine_helper_is_the_backend_the_url_names(tmp_path):
    engine = engine_for(f"sqlite:{tmp_path / 'x.db'}?mode=rwc", None)
    assert engine.dialect.name == "sqlite"


# ---------------------------------------------------------------------------
# A declared label rename: the downgrade renames it back once
# ---------------------------------------------------------------------------


def _trl_order(*, renamed: bool) -> None:
    if renamed:

        class TrlStatus(StrEnum):
            __ferro_renamed_labels__: ClassVar = {"cancelled": "canceled"}
            PAID = "paid"
            CANCELLED = "cancelled"

    else:

        class TrlStatus(StrEnum):
            PAID = "paid"
            CANCELED = "canceled"

    class TrlOrder(Model):
        id: int | None = Field(default=None, primary_key=True)
        status: TrlStatus


async def _trl_labels(db_url: str) -> list[str]:
    from ferro.raw import fetch_all

    await connect(db_url)
    try:
        async with engines.session():
            rows = await fetch_all(
                "SELECT e.enumlabel::text AS label FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname = 'trlstatus' AND n.nspname = current_schema() "
                "ORDER BY e.enumsortorder"
            )
    finally:
        reset_engine()
    return [r["label"] for r in rows]


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_label_rename_downgrade_renames_it_back_once(
    db_url, postgres_base_url, db_schema_name
):
    """``__ferro_renamed_labels__ = {"cancelled": "canceled"}`` on a live
    ``canceled``: the upgrade renames the label, and the downgrade renames it
    back exactly once (a second ``RENAME VALUE 'cancelled'`` fails, the label
    being ``canceled`` again), restoring the old label."""
    _trl_order(renamed=False)
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await execute('INSERT INTO "trlorder" ("status") VALUES (\'canceled\')')
    _rewind_registry()
    _trl_order(renamed=True)

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert upgrade.count("RENAME VALUE") == 1, upgrade
    assert_statement_in_code(
        "ALTER TYPE \"trlstatus\" RENAME VALUE 'canceled' TO 'cancelled'", upgrade
    )
    assert downgrade.count("RENAME VALUE") == 1, downgrade
    assert_statement_in_code(
        "ALTER TYPE \"trlstatus\" RENAME VALUE 'cancelled' TO 'canceled'", downgrade
    )

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _trl_labels(db_url) == ["paid", "cancelled"]
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    assert await _trl_labels(db_url) == ["paid", "canceled"]


# ---------------------------------------------------------------------------
# A redefined index and a removed foreign key (ADR-0051)
# ---------------------------------------------------------------------------


def _line(indexed: tuple[str, ...]) -> None:
    class Tr551Line(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (indexed,)

        id: int | None = Field(default=None, primary_key=True)
        order_id: int | None = None
        kind: str | None = None
        order: int | None = None
        id_kind: str | None = None


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_redefined_index_is_alembics_drop_then_create_both_ways(
    db_url, postgres_base_url, db_schema_name
):
    """``("order_id", "kind")`` and ``("order", "id_kind")`` share the name
    ``idx_tr551line_order_id_kind``: the bridge used to write nothing for
    the change. Now Alembic's drop and create, and the downgrade builds the
    live definition back."""
    _line(("order_id", "kind"))
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _line(("order", "id_kind"))

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert "op.drop_index('idx_tr551line_order_id_kind'" in upgrade, upgrade
    assert (
        "op.create_index('idx_tr551line_order_id_kind', 'tr551line', "
        "['order', 'id_kind']"
    ) in upgrade, upgrade
    assert (
        "op.create_index('idx_tr551line_order_id_kind', 'tr551line', "
        "['order_id', 'kind']"
    ) in downgrade, downgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _line(("order_id", "kind"))
    assert await _drift(db_url) == []


def _member(*, linked: bool) -> None:
    class Tr551Team(Model):
        id: int | None = Field(default=None, primary_key=True)
        if linked:
            members: Relation[list["Tr551Member"]] = BackRef()

    if linked:

        class Tr551Member(Model):
            id: int | None = Field(default=None, primary_key=True)
            team: Annotated[Tr551Team | None, ForeignKey(related_name="members")] = None

    else:

        class Tr551Member(Model):  # noqa: F811 — the other shape
            id: int | None = Field(default=None, primary_key=True)
            team_id: int | None = None


@pytest.mark.backend_matrix
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_a_foreign_key_removed_from_a_kept_column_is_drop_constraint_both_ways(
    db_url, postgres_base_url, db_schema_name
):
    """The column stays, its foreign key goes: the bridge used to write
    nothing. Now ``op.drop_constraint``, and the downgrade adds it back."""
    _member(linked=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _member(linked=False)

    upgrade, downgrade = autogenerate(db_url, postgres_base_url, db_schema_name)
    assert (
        "op.drop_constraint('fk_tr551member_team_id_tr551team', 'tr551member', "
        "type_='foreignkey')"
    ) in upgrade, upgrade
    assert "fk_tr551member_team_id_tr551team" in downgrade, downgrade

    run_revision(upgrade, db_url, postgres_base_url, db_schema_name)
    assert await _drift(db_url) == []
    run_revision(downgrade, db_url, postgres_base_url, db_schema_name)
    _rewind_registry()
    _member(linked=True)
    assert await _drift(db_url) == []


@pytest.mark.backend_matrix
@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_foreign_key_removed_on_sqlite_is_refused_naming_the_rebuild(
    db_url, postgres_base_url, db_schema_name
):
    """SQLite drops a table constraint only by rebuilding the table, which
    an Alembic revision cannot write: refused with the migration path."""
    _member(linked=True)
    await connect(db_url, auto_migrate=True)
    _rewind_registry()
    _member(linked=False)

    with pytest.raises(Exception, match=r"DropForeignKey .*ferro migrate new"):
        autogenerate(db_url, postgres_base_url, db_schema_name)
