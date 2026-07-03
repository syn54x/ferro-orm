"""FF-C C2 exit gate: zero catalog queries on steady-state CRUD.

Every operation used to issue up to three live ``pg_catalog`` /
``information_schema`` round-trips (F4). The statement-count instrument
(`_catalog_query_count_for_test`, incremented at the single choke point that
executes catalog SQL) proves that after the first operation against a table,
repeated CRUD issues **zero** catalog queries.
"""

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import pytest

import ferro
from ferro import BackRef, ManyToMany, Model, Relation, connect
from ferro._core import _catalog_query_count_for_test
from ferro.base import FerroField


@pytest.fixture
def clean_registry():
    from ferro import clear_registry, reset_engine
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    yield


class C2Priority(str, Enum):
    LOW = "low"
    HIGH = "high"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_steady_state_crud_issues_zero_catalog_queries(db_url, clean_registry):
    """The FF-C exit gate: after a warm-up op per table, repeated
    save/get/filter/count/update/delete/m2m traffic issues zero catalog
    round-trips."""

    class C2Tag(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        tasks: Relation[list["C2Task"]] = BackRef()

    class C2Task(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        priority: C2Priority = C2Priority.LOW
        run_id: uuid.UUID | None = None
        due: datetime | None = None
        tags: Relation[list["C2Tag"]] = ManyToMany(related_name="tasks")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        # Warm-up: touch every table (model tables and the m2m join table) once.
        tag = await C2Tag.create(label="warm")
        task = await C2Task.create(
            title="warm",
            priority=C2Priority.HIGH,
            run_id=uuid.uuid4(),
            due=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        )
        await task.tags.add(tag)
        await task.tags.remove(tag)
        await C2Task.where(lambda t: t.priority == C2Priority.HIGH).all()
        await C2Task.where(lambda t: t.title == "warm").count()
        await C2Task.where(lambda t: t.title == "nope").delete()
        await C2Task.where(lambda t: t.title == "warm").update(title="warmed")

        baseline = _catalog_query_count_for_test()

        # Steady state: the same operation mix, several times over.
        for i in range(3):
            row = await C2Task.create(
                title=f"steady-{i}",
                priority=C2Priority.HIGH,
                run_id=uuid.uuid4(),
                due=datetime(2026, 7, 2, 13, i, tzinfo=UTC),
            )
            fetched = await C2Task.get(row.id)
            assert fetched.priority is C2Priority.HIGH
            assert fetched.run_id == row.run_id

            await C2Task.where(lambda t: t.priority == C2Priority.HIGH).all()
            assert (
                await C2Task.where(lambda t, i=i: t.title == f"steady-{i}").count() == 1
            )
            await C2Task.where(lambda t, i=i: t.title == f"steady-{i}").update(
                title=f"steady-{i}-renamed"
            )

            row.title = f"steady-{i}-saved"
            await row.save()

            await row.tags.add(tag)
            assert await row.tags.count() == 1
            await row.tags.remove(tag)

            await row.delete()

        steady = _catalog_query_count_for_test()
        assert steady - baseline == 0, (
            f"steady-state CRUD issued {steady - baseline} catalog queries; expected 0"
        )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_enum_str_model_steady_state_and_correct_casts(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """The FF-B residual (Alembic-created native enum, model says ``str``)
    still binds with the ``::udt`` cast — served from the cache after the
    first op, with zero catalog queries in steady state."""

    class C2StrRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: str

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute("CREATE TYPE c2strrowstatus AS ENUM ('active', 'inactive')")
        conn.execute(
            """
            CREATE TABLE c2strrow (
                id BIGSERIAL PRIMARY KEY,
                status c2strrowstatus NOT NULL
            )
            """
        )
        conn.commit()

    await connect(db_url, auto_migrate=False)
    async with ferro.engines.session():

        first = await C2StrRow.create(status="active")
        assert (await C2StrRow.get(first.id)).status == "active"

        baseline = _catalog_query_count_for_test()
        for _ in range(3):
            row = await C2StrRow.create(status="inactive")
            fetched = await C2StrRow.get(row.id)
            assert fetched.status == "inactive"
        assert _catalog_query_count_for_test() == baseline


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_warm_up_issues_at_most_one_catalog_query_per_table(
    db_url, clean_registry
):
    """The cold path is bounded too: one combined catalog probe per table."""

    class C2Cold(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        due: datetime | None = None

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        before = _catalog_query_count_for_test()
        await C2Cold.create(name="first", due=datetime(2026, 7, 2, tzinfo=UTC))
        after_first = _catalog_query_count_for_test()
        assert after_first - before <= 1, (
            f"first op issued {after_first - before} catalog queries; expected ≤ 1"
        )

        await C2Cold.create(name="second")
        assert _catalog_query_count_for_test() == after_first


@pytest.mark.asyncio
async def test_sqlite_crud_issues_zero_catalog_queries(db_url, db_backend, clean_registry):
    """Non-Postgres backends must stay a cheap no-op: the counter never moves."""
    if db_backend != "sqlite":
        pytest.skip("sqlite-only assertion")

    class C2Lite(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        row = await C2Lite.create(name="a")
        await C2Lite.get(row.id)
        await C2Lite.where(lambda r: r.name == "a").count()
        assert _catalog_query_count_for_test() == 0


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_catalog_cache_invalidated_by_mid_session_migrate(
    db_url, clean_registry
):
    """connect → CRUD (populates cache) → migrate() adds an enum column
    (refresh_pool clears the cache) → the next CRUD binds against the new
    schema, not a stale snapshot."""

    class C2Grow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        # Non-optional: the legacy shadow migrate-planner resolves `Enum | None`
        # (anyOf) ADD COLUMN to varchar while the IR planner emits the native
        # enum — a pre-existing planner divergence unrelated to the catalog
        # cache; optional here would fail under FERRO_SHADOW_RUNTIME_STRICT.
        mood: C2Priority = C2Priority.LOW

    from ferro.raw import execute

    await connect(db_url, auto_migrate=False)
    async with ferro.engines.session():

        await execute(
            'CREATE TABLE "c2grow" ("id" bigserial PRIMARY KEY, "name" varchar NOT NULL)'
        )

        # Populate the cache for c2grow via reads (the mood column doesn't exist yet).
        assert await C2Grow.where(lambda g: g.name == "x").count() == 0
        assert await C2Grow.where(lambda g: g.name == "x").count() == 0

    # Mid-session schema change through the supported path.
    await ferro.migrate(updates=True)

    async with ferro.engines.session():

        # The new native-enum column must be visible to bind casting immediately;
        # a stale cache (no enum entry for "mood") would bind plain text and the
        # prepared INSERT would fail against the enum column.
        row = await C2Grow.create(name="grown", mood=C2Priority.HIGH)
        fetched = await C2Grow.get(row.id)
        assert fetched.mood is C2Priority.HIGH
