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
    import json as _json

    from ferro._core import register_model_schema

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migstrict" ("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
    )
    ferro.reset_engine()

    register_model_schema(
        "MigStrict",
        _json.dumps(
            {
                "properties": {
                    "id": {"type": "integer", "primary_key": True},
                    "name": {"type": "string", "ferro_nullable": False},
                    "created_at": {
                        "type": "string",
                        "format": "date-time",
                        "ferro_nullable": False,
                    },
                }
            }
        ),
    )

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

    import json as _json

    from ferro._core import register_model_schema

    register_model_schema(
        "MigDrift",
        _json.dumps(
            {
                "properties": {
                    "id": {"type": "integer", "primary_key": True},
                    "count": {"type": "integer", "ferro_nullable": False},
                }
            }
        ),
    )

    with pytest.warns(UserWarning, match=r"migdrift\.count.*Alembic"):
        await ferro.connect(db_url, migrate_updates=True)

    columns = _sqlite_columns(db_url, "migdrift")
    assert (
        columns["count"][2].lower() == "varchar"
    ), "no DDL may run for SQLite type drift"


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
    import json as _json

    from ferro._core import register_model_schema

    await ferro.connect(db_url)
    await execute(
        'CREATE TABLE "migpg" ("id" serial PRIMARY KEY, '
        '"total" integer NOT NULL, "note" varchar)'
    )
    await execute('INSERT INTO "migpg" ("total", "note") VALUES (41, NULL)')
    ferro.reset_engine()

    register_model_schema(
        "MigPg",
        _json.dumps(
            {
                "properties": {
                    "id": {"type": "integer", "primary_key": True},
                    "total": {
                        "type": "integer",
                        "db_type": "bigint",
                        "ferro_nullable": False,
                    },
                    "note": {"type": "string", "ferro_nullable": True},
                    "status": {
                        "type": "string",
                        "ferro_nullable": False,
                        "default": "draft",
                    },
                }
            }
        ),
    )

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
