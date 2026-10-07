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
    tables = sorted(
        {m["table_name"] for m in envelope["payload"]["models"]}
        | {
            m["renamed_from"]
            for m in envelope["payload"]["models"]
            if m.get("renamed_from")
        }
    )
    name = f"tr533_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        live, facts = await _core._live_schema_ir(name, json.dumps(tables))
        dialect = _core.connection_backend(name)
    finally:
        await _core._disconnect(name)
    assert dialect is not None
    plan = json.loads(
        _core._plan_from_ir(
            live, json.dumps(envelope), dialect, DESTRUCTIVE, True, facts
        )
    )
    return [op for op in plan["operations"] if op["statements"] or op["warnings"]]


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
        live, facts = await _core._live_schema_ir(name, json.dumps(["tr533ledger"]))
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
    """A database ferro's in-house migrations track (it carries the tracking
    tables) takes model changes from ``ferro migrate new``."""
    _card_v1()
    await connect(db_url, auto_migrate=True)
    name = f"tr533_{uuid.uuid4().hex}"
    await connect(db_url, name=name)
    try:
        await _core._ensure_tracking_tables(name, None)
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
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import sys

    monkeypatch.delenv("FERRO_CONFIG", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    yield root
    for module in set(sys.modules) - before:
        del sys.modules[module]


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
