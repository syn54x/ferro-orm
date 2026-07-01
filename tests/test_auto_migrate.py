from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import Field

import ferro
from ferro import BackRef, ManyToMany, Model, Relation
from ferro.base import FerroField

pytestmark = pytest.mark.backend_matrix


class AutoMigratedUser(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    username: str


@pytest.mark.asyncio
async def test_connect_with_auto_migrate(db_url):
    """Test that connect(auto_migrate=True) creates tables automatically."""
    # Reset engine to ensure clean state
    ferro.reset_engine()

    # Connect with auto_migrate=True
    # This should internally call the same logic as create_tables()
    await ferro.connect(db_url, auto_migrate=True)

    # We can verify it works by trying to call create_tables again
    # or by just ensuring it doesn't crash.
    # In a future step, when we have INSERT, we can verify the table exists.
    # For now, we are verifying the API signature and that it runs without error.
    assert True


@pytest.mark.asyncio
async def test_connect_without_auto_migrate(db_url):
    """Test that connect(auto_migrate=False) does not create tables (manual mode)."""
    ferro.reset_engine()

    await ferro.connect(db_url, auto_migrate=False)
    # Manual call still works
    await ferro.create_tables()
    assert True


@pytest.mark.asyncio
async def test_m2m_join_table_created_during_auto_migrate(db_url):
    """Verify that the many-to-many join table is created when auto_migrate=True.
    We clear registries, migrate a fresh in-memory DB, then use the M2M API; if the
    join table were not created, .add() would fail. No second connection needed."""
    from ferro import clear_registry, connect, reset_engine
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class Actor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        movies: Relation[list["Movie"]] = ManyToMany(related_name="actors")

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: Relation[list["Actor"]] = BackRef()

    await connect(db_url, auto_migrate=True)

    actor = await Actor.create(name="Alice")
    movie = await Movie.create(title="Matrix")
    await actor.movies.add(movie)

    linked = await actor.movies.all()
    assert len(linked) == 1
    assert linked[0].id == movie.id
    assert linked[0].title == "Matrix"
    assert await actor.movies.count() == 1

    reverse_linked = await movie.actors.all()
    assert [row.id for row in reverse_linked] == [actor.id]

    await actor.movies.remove(movie)
    assert await actor.movies.count() == 0

    movie_2 = await Movie.create(title="Reloaded")
    await actor.movies.add(movie, movie_2)
    assert await actor.movies.count() == 2
    await actor.movies.clear()
    assert await actor.movies.count() == 0


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_uuid_m2m_join_table_columns_inherit_pk_type_and_nullability(db_url):
    """Runtime join-table DDL should derive FK column metadata from source PKs."""
    from ferro import clear_registry, connect, reset_engine
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidActor(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        movies: Relation[list["UuidMovie"]] = ManyToMany(related_name="actors")

    class UuidMovie(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        title: str
        actors: Relation[list["UuidActor"]] = BackRef()

    await connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(uuidactor_movies)").fetchall()
    conn.close()

    columns = {row[1]: row for row in rows}
    assert columns["uuidactor_id"][2].upper() in {
        "UUID",
        "UUID_TEXT",
        "TEXT",
        "CHAR",
        "VARCHAR",
    }
    assert columns["uuidmovie_id"][2].upper() in {
        "UUID",
        "UUID_TEXT",
        "TEXT",
        "CHAR",
        "VARCHAR",
    }
    assert columns["uuidactor_id"][3] == 1
    assert columns["uuidmovie_id"][3] == 1


@pytest.mark.asyncio
async def test_uuid_m2m_relationship_query_serializes_source_id(db_url):
    """UUID source PKs in M2M contexts should serialize for all query operations."""
    from ferro import Field as FerroFieldFn
    from ferro import clear_registry, connect, reset_engine
    from ferro.models import transaction
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidTag(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        name: str = ""
        posts: Relation[list["UuidPost"]] = BackRef()

    class UuidPost(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        title: str = ""
        tags: Relation[list[UuidTag]] = ManyToMany(related_name="posts")

    await connect(db_url, auto_migrate=True)

    post = await UuidPost.create(title="Hello")
    tag = await UuidTag.create(name="python")

    await post.tags.add(tag)

    linked = await post.tags.all()
    assert [row.id for row in linked] == [tag.id]
    assert await post.tags.count() == 1

    reverse_linked = await tag.posts.all()
    assert [row.id for row in reverse_linked] == [post.id]

    await post.tags.remove(tag)
    assert await post.tags.count() == 0

    tag_2 = await UuidTag.create(name="orm")
    await post.tags.add(tag, tag_2)
    assert await post.tags.count() == 2
    await post.tags.clear()
    assert await post.tags.count() == 0

    async with transaction():
        await post.tags.add(tag)
        assert await post.tags.count() == 1
        await post.tags.remove(tag)
        assert await post.tags.count() == 0

    assert await post.tags.count() == 0


# ---------------------------------------------------------------------------
# migrate_updates / migrate_destructive (issue #68)
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

from ferro.raw import execute, fetch_all  # noqa: E402


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


def _sqlite_columns(db_url: str, table: str) -> dict[str, tuple]:
    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        conn.close()
    return {row[1]: row for row in rows}


def _sqlite_index_names(db_url: str, table: str) -> set[str]:
    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    finally:
        conn.close()
    return {row[1] for row in rows}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_adds_missing_columns_and_hydrates(
    db_url, db_backend, clean_registry
):
    """The issue #67/#68 repro shape: a pre-existing narrow table gains the
    model's new columns on connect, and the very next ORM query hydrates
    existing rows with the new fields as None (no panic, no silent empty
    result)."""

    class MigInvoice(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        number: str
        paid_date: Annotated[date | None, FerroField(db_type="date")] = None
        memo: str | None = None

    # Bootstrap the OLD (narrow) schema by hand, as an older release would have.
    await ferro.connect(db_url)
    if db_backend == "sqlite":
        await execute(
            'CREATE TABLE "miginvoice" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "number" varchar NOT NULL)'
        )
    else:
        await execute(
            'CREATE TABLE "miginvoice" ("id" serial PRIMARY KEY, "number" varchar NOT NULL)'
        )
    await execute('INSERT INTO "miginvoice" ("number") VALUES (\'INV-1\')')
    ferro.reset_engine()

    await ferro.connect(db_url, migrate_updates=True)

    rows = await MigInvoice.all()
    assert len(rows) == 1
    assert rows[0].number == "INV-1"
    assert rows[0].paid_date is None
    assert rows[0].memo is None

    # The new columns are usable immediately.
    inv = await MigInvoice.create(
        number="INV-2", paid_date=date(2026, 1, 15), memo="paid"
    )
    fetched = await MigInvoice.get(inv.id)
    assert fetched.paid_date == date(2026, 1, 15)
    assert fetched.memo == "paid"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_manual_migrate_on_live_pool_refreshes_cached_statements(
    db_url, db_backend, clean_registry
):
    """`ferro.migrate()` on a live pool must work even when the same query was
    already prepared (and cached) against the pre-migration schema — the
    engine refreshes its pool after DDL (issue #67)."""

    class MigReport(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        summary: str | None = None

    await ferro.connect(db_url)
    if db_backend == "sqlite":
        await execute(
            'CREATE TABLE "migreport" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL)'
        )
    else:
        await execute(
            'CREATE TABLE "migreport" ("id" serial PRIMARY KEY, "title" varchar NOT NULL)'
        )
    await execute('INSERT INTO "migreport" ("title") VALUES (\'Q1\')')

    # Prepare (and cache) the SELECT against the narrow schema.
    rows_before = await MigReport.all()
    assert len(rows_before) == 1

    await ferro.migrate()

    # Same query again: without the pool refresh this panics in the sqlx
    # worker and silently returns zero rows on SQLite.
    rows_after = await MigReport.all()
    assert len(rows_after) == 1
    assert rows_after[0].title == "Q1"
    assert rows_after[0].summary is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_migrate_updates_not_null_without_default_fails_loudly(
    db_url, clean_registry
):
    """A new NOT NULL field with no literal default cannot backfill existing
    rows; connecting must fail with an error naming the field."""
    from datetime import datetime

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migstrict" ("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
    )
    ferro.reset_engine()

    class MigStrict(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        created_at: datetime

    with pytest.raises(ValueError, match=r"migstrict\.created_at"):
        await ferro.connect(db_url, migrate_updates=True)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_type_drift_warns_and_leaves_column_untouched(
    db_url, clean_registry
):
    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migdrift" '
        '("id" integer PRIMARY KEY AUTOINCREMENT, "count" varchar NOT NULL)'
    )
    ferro.reset_engine()

    class MigDrift(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int

    with pytest.warns(UserWarning, match=r"migdrift\.count.*Alembic"):
        await ferro.connect(db_url, migrate_updates=True)

    columns = _sqlite_columns(db_url, "migdrift")
    assert (
        columns["count"][2].lower() == "varchar"
    ), "no DDL may run for SQLite type drift"


def _pg_live_type(base_url: str, schema: str, table: str, column: str) -> str:
    import psycopg

    with psycopg.connect(base_url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (schema, table, column),
        ).fetchone()
    return row[0] if row else "<absent>"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_datetime_over_external_naive_timestamp_warns_and_skips(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """An external plain `timestamp` column + a `datetime` model must NOT be
    silently rewritten to `timestamptz`; auto-migrate warns and leaves it. (#154)"""
    import datetime as dt

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "event" '
        '("id" serial PRIMARY KEY, "occurred_at" timestamp NOT NULL)'
    )
    ferro.reset_engine()

    class Event(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        occurred_at: dt.datetime

    with pytest.warns(UserWarning, match=r"event\.occurred_at.*db_type.*Alembic"):
        await ferro.connect(db_url, migrate_updates=True)

    # The external column is untouched — no silent reinterpretation.
    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "event", "occurred_at")
        == "timestamp without time zone"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_datetime_db_type_override_keeps_naive_no_drift(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """The escape hatch: db_type="timestamp" over an external naive column
    produces no drift, no warning, and no rewrite. (#154)"""
    import datetime as dt
    import warnings as _warnings

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "event2" '
        '("id" serial PRIMARY KEY, "occurred_at" timestamp NOT NULL)'
    )
    ferro.reset_engine()

    class Event2(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        occurred_at: Annotated[dt.datetime, FerroField(db_type="timestamp")]

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UserWarning)  # any drift warning -> failure
        await ferro.connect(db_url, migrate_updates=True)

    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "event2", "occurred_at")
        == "timestamp without time zone"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_ferro_created_timestamptz_no_drift(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """Control: a Ferro-created (timestamptz) column reconnects with no drift
    and no warning — Ferro-managed date-time columns are unaffected. (#154)"""
    import datetime as dt
    import warnings as _warnings

    class Event3(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        occurred_at: dt.datetime

    await ferro.connect(db_url, auto_migrate=True)  # Ferro creates it -> timestamptz
    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "event3", "occurred_at")
        == "timestamp with time zone"
    )
    ferro.reset_engine()

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UserWarning)
        await ferro.connect(db_url, migrate_updates=True)

    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "event3", "occurred_at")
        == "timestamp with time zone"
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_destructive_drops_removed_columns(
    db_url, db_backend, clean_registry
):
    class MigSlim(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url)
    if db_backend == "sqlite":
        await execute(
            'CREATE TABLE "migslim" ("id" integer PRIMARY KEY AUTOINCREMENT, '
            '"name" varchar NOT NULL, "legacy_notes" text)'
        )
    else:
        await execute(
            'CREATE TABLE "migslim" ("id" serial PRIMARY KEY, '
            '"name" varchar NOT NULL, "legacy_notes" text)'
        )
    await execute(
        'INSERT INTO "migslim" ("name", "legacy_notes") VALUES (\'keep\', \'bye\')'
    )
    ferro.reset_engine()

    # Without the flag the extra column is untouched.
    await ferro.connect(db_url, migrate_updates=True)
    rows = await fetch_all('SELECT * FROM "migslim"')
    assert "legacy_notes" in rows[0]
    ferro.reset_engine()

    await ferro.connect(db_url, migrate_destructive=True)
    rows = await fetch_all('SELECT * FROM "migslim"')
    assert len(rows) == 1
    assert rows[0]["name"] == "keep", "surviving data must be intact"
    assert "legacy_notes" not in rows[0]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_destructive_drop_of_indexed_column_drops_index_first(
    db_url, clean_registry
):
    class MigIdx(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migidx" ("id" integer PRIMARY KEY AUTOINCREMENT, '
        '"name" varchar NOT NULL, "old_status" varchar)'
    )
    await execute('CREATE INDEX "idx_migidx_old_status" ON "migidx" ("old_status")')
    await execute(
        'INSERT INTO "migidx" ("name", "old_status") VALUES (\'keep\', \'x\')'
    )
    ferro.reset_engine()

    await ferro.connect(db_url, migrate_destructive=True)

    columns = _sqlite_columns(db_url, "migidx")
    assert "old_status" not in columns
    assert "idx_migidx_old_status" not in _sqlite_index_names(db_url, "migidx")
    rows = await fetch_all('SELECT * FROM "migidx"')
    assert rows[0]["name"] == "keep"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_destructive_refuses_unique_constraint_column_on_sqlite(
    db_url, clean_registry
):
    class MigUq(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "miguq" ("id" integer PRIMARY KEY AUTOINCREMENT, '
        '"name" varchar NOT NULL, "old_code" varchar UNIQUE)'
    )
    ferro.reset_engine()

    with pytest.raises(ValueError, match=r"miguq\.old_code.*UNIQUE.*Alembic"):
        await ferro.connect(db_url, migrate_destructive=True)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_added_indexed_and_unique_columns_get_their_indexes(
    db_url, clean_registry
):
    class MigIndexed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        status: Annotated[str | None, FerroField(index=True)] = None
        slug: Annotated[str | None, FerroField(unique=True)] = None

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migindexed" '
        '("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
    )
    ferro.reset_engine()

    with pytest.warns(UserWarning, match="uq_migindexed_slug"):
        await ferro.connect(db_url, migrate_updates=True)

    index_names = _sqlite_index_names(db_url, "migindexed")
    assert "idx_migindexed_status" in index_names
    assert "uq_migindexed_slug" in index_names


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_postgres_type_and_nullability_reconciliation(db_url, clean_registry):
    """Postgres gets native ALTER COLUMN: type changes via USING cast (with
    existing data), SET NOT NULL, and no lingering server default after a
    backfilled NOT NULL add."""
    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migpg" ("id" serial PRIMARY KEY, '
        '"total" integer NOT NULL, "note" varchar)'
    )
    await execute('INSERT INTO "migpg" ("total", "note") VALUES (41, NULL)')
    ferro.reset_engine()

    # Assignment style keeps this model valid. The #155 trap is an *invalid*
    # annotation (e.g. `Annotated[str, FerroField(default=...)]` -- FerroField
    # has no `default`; that kwarg belongs to the assignment-side `ferro.Field`).
    # Under Python 3.14 / PEP 649 such a broken annotation is deferred and, before
    # the #155 fix, was swallowed -> all annotations dropped -> Pydantic flagged
    # the first assigned field as non-annotated. Defaults belong on the assignment
    # side, as below, which never defers a broken expression.
    class MigPg(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        total: int = ferro.Field(db_type="bigint")
        note: str | None = None
        status: str = ferro.Field(default="draft")

    await ferro.connect(db_url, migrate_updates=True)

    rows = await fetch_all(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'migpg'"
    )
    by_name = {row["column_name"]: row for row in rows}
    assert by_name["total"]["data_type"] == "bigint", "integer -> bigint via USING cast"
    assert by_name["status"]["is_nullable"] == "NO"
    assert (
        by_name["status"]["column_default"] is None
    ), "backfill default must not linger"

    data = await fetch_all('SELECT "total", "status" FROM "migpg"')
    assert data[0]["total"] == 41, "existing data survives the type change"
    assert (
        data[0]["status"] == "draft"
    ), "existing rows backfilled with the literal default"


# ---------------------------------------------------------------------------
# Index reconciliation (issue #144)
# ---------------------------------------------------------------------------


def _live_index_names(db_url: str, db_backend: str, table: str) -> set[str]:
    """Return the set of index names on *table* in the live DB.

    For SQLite we use PRAGMA index_list (synchronous raw driver).
    For Postgres we query pg_indexes via the same synchronous psycopg path used
    elsewhere in this file.  The postgres_base_url / db_schema_name fixtures are
    not available here so we parse the search_path from db_url directly.
    """
    if db_backend == "sqlite":
        return _sqlite_index_names(db_url, table)

    # Postgres: extract connection params from the async URL.
    # URL form: postgres://user:pass@host:port/dbname?options=-c search_path=<schema>
    import re
    import psycopg

    m = re.search(r"search_path=([^&]+)", db_url)
    schema = m.group(1) if m else "public"
    # Build a synchronous libpq-style DSN by replacing the async scheme.
    sync_url = db_url.replace("postgres://", "postgresql://", 1)
    # Strip the options query param for psycopg (it handles search_path separately).
    base_url = sync_url.split("?")[0]

    with psycopg.connect(base_url, options=f"-c search_path={schema}") as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (schema, table),
        ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_index_reconcile_adds_composite_index_to_existing_table(
    db_url, db_backend, clean_registry
):
    """migrate_updates adds a composite Ferro-named index to a pre-existing
    table that was created without it."""
    from typing import ClassVar

    class IdxCompModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        col_a: int
        col_b: int

    # Create the table without any indexes.
    await ferro.connect(db_url)
    if db_backend == "sqlite":
        await execute(
            'CREATE TABLE "idxcompmodel" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, '
            '"col_a" integer NOT NULL, "col_b" integer NOT NULL)'
        )
    else:
        await execute(
            'CREATE TABLE "idxcompmodel" '
            '("id" serial PRIMARY KEY, '
            '"col_a" integer NOT NULL, "col_b" integer NOT NULL)'
        )
    ferro.reset_engine()

    # Re-register a NEW model class with the composite index annotation.
    from ferro import clear_registry
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class IdxCompModel(Model):  # noqa: F811 — intentional redefinition
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("col_a", "col_b"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        col_a: int
        col_b: int

    await ferro.connect(db_url, migrate_updates=True)

    names = _live_index_names(db_url, db_backend, "idxcompmodel")
    assert "idx_idxcompmodel_col_a_col_b" in names, (
        f"expected composite index idx_idxcompmodel_col_a_col_b, got: {names}"
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_index_reconcile_adds_single_column_index_to_existing_column(
    db_url, db_backend, clean_registry
):
    """migrate_updates creates idx_<table>_<col> when index=True is added to an
    existing column that was originally created without an index."""

    class IdxSingleModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: str

    # Create table with 'status' but no index on it.
    await ferro.connect(db_url)
    if db_backend == "sqlite":
        await execute(
            'CREATE TABLE "idxsinglemodel" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "status" varchar NOT NULL)'
        )
    else:
        await execute(
            'CREATE TABLE "idxsinglemodel" '
            '("id" serial PRIMARY KEY, "status" varchar NOT NULL)'
        )
    ferro.reset_engine()

    # Re-register with index=True on the existing column.
    from ferro import clear_registry
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class IdxSingleModel(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: Annotated[str, FerroField(index=True)]

    await ferro.connect(db_url, migrate_updates=True)

    names = _live_index_names(db_url, db_backend, "idxsinglemodel")
    assert "idx_idxsinglemodel_status" in names, (
        f"expected idx_idxsinglemodel_status, got: {names}"
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_index_reconcile_noop_when_index_already_present(
    db_url, db_backend, clean_registry
):
    """Running migrate_updates a second time when the index already exists must
    be a no-op: the index remains and auto-migrate does not fail (false-alarm guard)."""
    from typing import ClassVar

    class IdxNoopModel(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("x", "y"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        x: int
        y: int

    # First connect creates the table + index.
    await ferro.connect(db_url, auto_migrate=True)
    names_after_first = _live_index_names(db_url, db_backend, "idxnoopmodel")
    assert "idx_idxnoopmodel_x_y" in names_after_first
    ferro.reset_engine()

    # Second connect — same model, same index already present.
    await ferro.connect(db_url, migrate_updates=True)

    names_after_second = _live_index_names(db_url, db_backend, "idxnoopmodel")
    assert "idx_idxnoopmodel_x_y" in names_after_second, (
        "index must survive second migrate_updates pass (no-op guard)"
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_index_reconcile_destructive_drops_removed_composite_index(
    db_url, db_backend, clean_registry
):
    """When a composite index is removed from the model:
    - migrate_destructive=True drops it.
    - migrate_updates=True (non-destructive) leaves it intact."""
    from typing import ClassVar

    class IdxDropModel(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("p", "q"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        p: int
        q: int

    # Bootstrap with the index present.
    await ferro.connect(db_url, auto_migrate=True)
    names = _live_index_names(db_url, db_backend, "idxdropmodel")
    assert "idx_idxdropmodel_p_q" in names
    ferro.reset_engine()

    # Re-register model WITHOUT the composite index.
    from ferro import clear_registry
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class IdxDropModel(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        p: int
        q: int

    # Non-destructive: index must remain.
    await ferro.connect(db_url, migrate_updates=True)
    names_after_updates = _live_index_names(db_url, db_backend, "idxdropmodel")
    assert "idx_idxdropmodel_p_q" in names_after_updates, (
        "non-destructive pass must leave orphaned ferro index intact"
    )
    ferro.reset_engine()

    # Destructive: index must be dropped.
    await ferro.connect(db_url, migrate_destructive=True)
    names_after_destructive = _live_index_names(db_url, db_backend, "idxdropmodel")
    assert "idx_idxdropmodel_p_q" not in names_after_destructive, (
        "migrate_destructive must drop the orphaned ferro index"
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_index_reconcile_user_index_survives_auto_migrate(
    db_url, db_backend, clean_registry
):
    """A hand-created user index (name does NOT start with idx_/uq_) must
    survive both non-destructive and destructive auto-migrate passes unchanged."""
    from typing import ClassVar

    class IdxUserModel(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("m", "n"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        m: int
        n: int

    # Create table with a Ferro composite index AND a custom user index.
    await ferro.connect(db_url, auto_migrate=True)
    await execute('CREATE INDEX "my_custom_idx" ON "idxusermodel" ("m")')

    names_initial = _live_index_names(db_url, db_backend, "idxusermodel")
    assert "idx_idxusermodel_m_n" in names_initial
    assert "my_custom_idx" in names_initial
    ferro.reset_engine()

    # Non-destructive pass: both indexes must survive.
    await ferro.connect(db_url, migrate_updates=True)
    names_after_updates = _live_index_names(db_url, db_backend, "idxusermodel")
    assert "idx_idxusermodel_m_n" in names_after_updates
    assert "my_custom_idx" in names_after_updates, (
        "user index must survive non-destructive auto-migrate"
    )
    ferro.reset_engine()

    # Destructive pass: Ferro index still present (model still has it), user index still present.
    await ferro.connect(db_url, migrate_destructive=True)
    names_after_destructive = _live_index_names(db_url, db_backend, "idxusermodel")
    assert "idx_idxusermodel_m_n" in names_after_destructive
    assert "my_custom_idx" in names_after_destructive, (
        "user index must survive destructive auto-migrate"
    )


# ---------------------------------------------------------------------------
# IR cutover — Python SchemaIR over FFI (issue #141 Task 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_uuid_pk_derived_second_pass_is_noop(db_url, db_backend, clean_registry):
    """End-to-end Example-1 guard (issue #141 Task 4):
    A model with a derived UUID primary key (default_factory=uuid4) connected via
    migrate_updates twice must produce no DDL and no warning on the second pass.
    This exercises the Python→Rust SchemaIR FFI path: the Rust runtime must
    recognise the table as already up-to-date after the first migrate pass."""
    import warnings as _warnings

    class UuidPkItem(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        label: str

    # First connect: creates the table (auto_migrate) and runs migrate_updates.
    await ferro.connect(db_url, migrate_updates=True)
    ferro.reset_engine()

    # Second connect: same model, same schema — must be a complete no-op.
    from ferro import clear_registry
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidPkItem(Model):  # noqa: F811 — intentional re-declaration for second connect
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        label: str

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)

    ferro_warnings = [
        w for w in caught if issubclass(w.category, UserWarning)
        and "ferro auto-migrate" in str(w.message)
    ]
    assert ferro_warnings == [], (
        f"Expected no ferro auto-migrate warnings on second pass; got: "
        f"{[str(w.message) for w in ferro_warnings]}"
    )

    # Additionally verify that no DDL ran — the live `id` column must still carry
    # the UUID storage type from the first pass (not a replacement type).
    if db_backend == "sqlite":
        columns = _sqlite_columns(db_url, "uuidpkitem")
        id_type = columns["id"][2].lower()
        assert "char" in id_type or "uuid" in id_type, (
            f"Expected uuid/char storage type for `id` after second pass; got '{id_type}' "
            "(a different type would mean DDL ran on the second pass)"
        )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_uuid_pk_derived_drift_is_stable(db_url, db_backend, clean_registry):
    """Drift check: a UUID PK model connected with migrate_updates multiple times
    must not report any DDL drift. The storage-token comparison must see the
    uuid_text / uuid declared type as stable after the first connect."""
    import warnings as _warnings

    class UuidDriftModel(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str

    # Bootstrap: create the table.
    await ferro.connect(db_url, auto_migrate=True)
    ferro.reset_engine()

    # First migrate_updates pass — should apply nothing (fresh table).
    from ferro import clear_registry
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidDriftModel(Model):  # noqa: F811
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str

    with _warnings.catch_warnings(record=True) as caught_first:
        _warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)

    ferro.reset_engine()

    # Second migrate_updates pass — must be identical to the first (idempotent).
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidDriftModel(Model):  # noqa: F811
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str

    with _warnings.catch_warnings(record=True) as caught_second:
        _warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)

    drift_warnings_first = [
        w for w in caught_first
        if issubclass(w.category, UserWarning) and "ferro auto-migrate" in str(w.message)
    ]
    drift_warnings_second = [
        w for w in caught_second
        if issubclass(w.category, UserWarning) and "ferro auto-migrate" in str(w.message)
    ]

    assert drift_warnings_first == [], (
        f"UUID PK must not trigger drift warning on first migrate_updates: "
        f"{[str(w.message) for w in drift_warnings_first]}"
    )
    assert drift_warnings_second == [], (
        f"UUID PK must not trigger drift warning on second migrate_updates: "
        f"{[str(w.message) for w in drift_warnings_second]}"
    )


# ---------------------------------------------------------------------------
# Task 3 (#153): create_tables() consumes the SchemaIR modelset and re-pushes
# it at the Python boundary, so a model defined AFTER connect() is created.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tables_pushes_modelset_for_model_defined_after_connect(
    db_url, clean_registry
):
    """create_tables() must re-push the current registry SchemaIR so a model
    declared *after* connect() (when the connect-time snapshot did not include
    it) still gets created. Proves the standalone create path is not relying on
    a stale connect-time modelset."""

    # Connect first, with NO models registered yet — the connect-time modelset
    # snapshot is empty.
    await ferro.connect(db_url, auto_migrate=False)

    # Now define a model AFTER connect().
    class LateModel(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        label: str

    # create_tables() must compile + push the up-to-date registry IR before the
    # Rust create runs, otherwise this table never gets created.
    await ferro.create_tables()

    # If the table exists, an INSERT + SELECT round-trips cleanly.
    row = await LateModel.create(id=1, label="hello")
    assert row.id == 1
    fetched = await LateModel.get(1)
    assert fetched.label == "hello"


@pytest.mark.asyncio
async def test_create_tables_fails_loud_when_modelset_cleared(db_url, clean_registry):
    """With the SchemaIR modelset deliberately cleared and the re-push bypassed,
    the Rust create path must raise loudly rather than silently creating
    nothing. Guards the fail-loud contract on the internal create entrypoint."""
    # The raw, un-wrapped Rust pyfunction does NOT re-push the modelset.
    from ferro._core import (
        _clear_schema_ir_modelset_for_test,
        create_tables as _raw_create_tables,
    )

    class GuardModel(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        name: str

    await ferro.connect(db_url, auto_migrate=False)

    # Clear the pushed modelset, then call the raw internal create (which does
    # NOT re-push) to prove the Rust side fails loud on a missing modelset.
    _clear_schema_ir_modelset_for_test()

    with pytest.raises(RuntimeError, match="modelset not set"):
        await _raw_create_tables()
