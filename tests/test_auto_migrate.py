import logging
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
    async with ferro.engines.session():

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
    async with ferro.engines.session():
        # Manual call still works
        await ferro.create_tables()
        assert True


@pytest.mark.asyncio
async def test_m2m_join_table_created_during_auto_migrate(db_url):
    """Verify that the many-to-many join table is created when auto_migrate=True.
    We clear registries, migrate a fresh in-memory DB, then use the M2M API; if the
    join table were not created, .add() would fail. No second connection needed."""
    from ferro import clear_registry, connect, reset_engine
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class Actor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        movies: Relation[list["Movie"]] = ManyToMany(related_name="actors")

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: Relation[list["Actor"]] = BackRef()

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

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
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class UuidActor(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        movies: Relation[list["UuidMovie"]] = ManyToMany(related_name="actors")

    class UuidMovie(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        title: str
        actors: Relation[list["UuidActor"]] = BackRef()

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        import sqlite3

        db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
        conn = sqlite3.connect(db_path)
        rows = conn.execute("PRAGMA table_info(uuidactor_movies)").fetchall()
        conn.close()

        columns = {row[1]: row for row in rows}
        assert columns["uuidactor_id"][2].upper() in {
            "UUID",
            "CHAR(32)",
            "TEXT",
            "CHAR",
            "VARCHAR",
        }
        assert columns["uuidmovie_id"][2].upper() in {
            "UUID",
            "CHAR(32)",
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
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class UuidTag(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        name: str = ""
        posts: Relation[list["UuidPost"]] = BackRef()

    class UuidPost(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        title: str = ""
        tags: Relation[list[UuidTag]] = ManyToMany(related_name="posts")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

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
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():

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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "migdrift" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "count" varchar NOT NULL)'
        )
    ferro.reset_engine()

    class MigDrift(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        count: int

    with pytest.warns(UserWarning, match=r"migdrift\.count.*ferro migrate new"):
        await ferro.connect(db_url, migrate_updates=True)

    columns = _sqlite_columns(db_url, "migdrift")
    assert (
        columns["count"][2].lower() == "varchar"
    ), "no DDL may run for SQLite type drift"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_bytes_field_does_not_false_positive_blob_drift(
    db_url, clean_registry
):
    """A fresh model with a `bytes` field creates a BLOB column; auto-migrate must
    NOT warn that it is 'declared text but the model expects blob'. The model and
    the DB agree. (#165)"""
    import warnings

    class Document(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        name: str = ""
        data: bytes = b""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)

    blob_warnings = [
        str(w.message) for w in caught if "blob" in str(w.message).lower()
    ]
    assert not blob_warnings, f"spurious BLOB drift warning(s): {blob_warnings}"

    # The column really is a BLOB — the fix did not mask a real difference.
    columns = _sqlite_columns(db_url, "document")
    assert columns["data"][2].lower() == "blob"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_genuine_text_to_blob_diff_still_warns(
    db_url, clean_registry
):
    """Control: a pre-existing TEXT column adopted by a `bytes` model is a REAL
    difference and must still warn — the fix only silences blob==blob. (#165)"""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "attachment" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "payload" text NOT NULL)'
        )
    ferro.reset_engine()

    class Attachment(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        payload: bytes = b""

    with pytest.warns(UserWarning, match=r"attachment\.payload.*expects 'blob'"):
        await ferro.connect(db_url, migrate_updates=True)

    # No DDL runs for SQLite type drift — the column is left as declared.
    columns = _sqlite_columns(db_url, "attachment")
    assert columns["payload"][2].lower() == "text"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_bytes_field_bytea_no_drift(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """Mirror-image control: on Postgres a `bytes` field maps to `bytea`, which
    already round-trips through introspection — no warning, no drift, ever. (#165)"""
    import warnings

    class DocPg(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        name: str = ""
        data: bytes = b""

    await ferro.connect(db_url, migrate_updates=True)  # creates docpg -> bytea
    ferro.reset_engine()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)  # reconnect: must be quiet
    blob_warnings = [
        str(w.message) for w in caught if "blob" in str(w.message).lower()
    ]
    assert not blob_warnings, f"unexpected BLOB drift warning(s): {blob_warnings}"

    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "docpg", "data") == "bytea"
    )


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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        rows = await fetch_all('SELECT * FROM "migslim"')
        assert "legacy_notes" in rows[0]
    ferro.reset_engine()

    await ferro.connect(db_url, migrate_destructive=True)
    async with ferro.engines.session():
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
    async with ferro.engines.session():
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
    async with ferro.engines.session():

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
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "miguq" ("id" integer PRIMARY KEY AUTOINCREMENT, '
            '"name" varchar NOT NULL, "old_code" varchar UNIQUE)'
        )
    ferro.reset_engine()

    with pytest.raises(ValueError, match=r"miguq\.old_code.*UNIQUE.*ferro migrate new"):
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
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "migindexed" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
        )
    ferro.reset_engine()

    # The uq_ index is the canonical unique shape on both dialects since
    # FF-B B4/D1 — no SQLite-compromise warning is emitted anymore.
    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():

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
    async with ferro.engines.session():
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
    async with ferro.engines.session():

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


@pytest.mark.asyncio
async def test_json_factory_default_backfills_existing_rows(
    db_url, db_backend, clean_registry
):
    """#373: default_factory=dict is a NOT NULL ADD COLUMN backfill literal."""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        if db_backend == "sqlite":
            await execute(
                'CREATE TABLE "migturns" '
                '("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
            )
        else:
            await execute(
                'CREATE TABLE "migturns" ("id" serial PRIMARY KEY, "name" varchar NOT NULL)'
            )
        await execute('INSERT INTO "migturns" ("name") VALUES (\'alpha\')')
    ferro.reset_engine()

    class MigTurns(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        name: str
        turns: dict[str, dict] = ferro.Field(default_factory=dict)

    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():
        rows = await MigTurns.all()
        assert len(rows) == 1
        assert rows[0].name == "alpha"
        assert rows[0].turns == {}

        created = await MigTurns.create(name="beta")
        assert created.turns == {}

        if db_backend == "postgres":
            info = await fetch_all(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'migturns' "
                "AND column_name = 'turns'"
            )
            assert info[0]["column_default"] is None, "backfill default must not linger"
        else:
            dflt = _sqlite_columns(db_url, "migturns")["turns"][4]
            assert dflt == "'{}'", "SQLite cannot DROP DEFAULT; backfill lingers"


@pytest.mark.asyncio
async def test_json_static_object_default_backfills_existing_rows(
    db_url, db_backend, clean_registry
):
    """#373: Field(default={}) on json-family storage is the same literal."""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        if db_backend == "sqlite":
            await execute(
                'CREATE TABLE "migflags" '
                '("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
            )
        else:
            await execute(
                'CREATE TABLE "migflags" ("id" serial PRIMARY KEY, "name" varchar NOT NULL)'
            )
        await execute('INSERT INTO "migflags" ("name") VALUES (\'alpha\')')
    ferro.reset_engine()

    class MigFlags(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        name: str
        flags: dict[str, str] = ferro.Field(default={})

    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():
        rows = await MigFlags.all()
        assert rows[0].flags == {}


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_json_factory_is_not_a_create_table_server_default(db_url, clean_registry):
    """Field defaults stay client-side on CREATE TABLE (#373)."""

    class MigFreshTurns(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        turns: dict[str, dict] = ferro.Field(default_factory=dict)

    await ferro.connect(db_url, auto_migrate=True)
    dflt = _sqlite_columns(db_url, "migfreshturns")["turns"][4]
    assert dflt is None


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
    async with ferro.engines.session():
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
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    class IdxCompModel(Model):  # noqa: F811 — intentional redefinition
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("col_a", "col_b"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        col_a: int
        col_b: int

    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():

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
    async with ferro.engines.session():
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
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    class IdxSingleModel(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: Annotated[str, FerroField(index=True)]

    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():

        names = _live_index_names(db_url, db_backend, "idxsinglemodel")
        assert "idx_idxsinglemodel_status" in names, (
            f"expected idx_idxsinglemodel_status, got: {names}"
        )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_adds_columns_before_composite_unique_referencing_them(
    db_url, db_backend, clean_registry
):
    """Issue #324: one migrate_updates pass over an existing populated table
    must add new columns before creating a composite unique that references
    them. The create pass must leave the existing table entirely alone —
    firing the unique's DDL there fails with "column does not exist" before
    the reconciliation pass can add the columns."""
    from typing import ClassVar

    class ProvTxn(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        account_id: int

    # Phase 1: the pre-upgrade release creates and populates the table.
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await ProvTxn.create(account_id=1)
    ferro.reset_engine()

    from ferro import clear_registry
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    # Phase 2: the upgrade declares two new columns and a composite unique
    # spanning one existing and one new column — the single-deploy shape.
    class ProvTxn(Model):  # noqa: F811 — intentional redefinition
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("account_id", "provider_transaction_id"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        account_id: int
        provider_transaction_id: str | None = None
        pending: bool = False

    await ferro.connect(db_url, auto_migrate=True, migrate_updates=True)
    async with ferro.engines.session():
        rows = await ProvTxn.all()
        assert len(rows) == 1
        assert rows[0].provider_transaction_id is None
        assert rows[0].pending is False

        names = _live_index_names(db_url, db_backend, "provtxn")
        assert "uq_provtxn_account_id_provider_transaction_id" in names, (
            f"expected uq_provtxn_account_id_provider_transaction_id, got: {names}"
        )

        # The unique must span BOTH columns. (SQLite's double-quoted-string
        # fallback can silently create an index over a string *constant* when
        # the column doesn't exist yet — same account with two distinct
        # provider ids must be allowed.)
        await ProvTxn.create(account_id=2, provider_transaction_id="p1")
        await ProvTxn.create(account_id=2, provider_transaction_id="p2")

        from ferro.exceptions import UniqueViolationError

        with pytest.raises(UniqueViolationError):
            await ProvTxn.create(account_id=2, provider_transaction_id="p1")


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_migrate_updates_rebuilds_fk_on_delete_drift(db_url, clean_registry):
    """Issue #325: changing a ForeignKey's on_delete on an existing model must
    rebuild the live constraint, not silently keep the old action. The Pinch
    shape: CASCADE (the default) shipped, then the upgrade declares SET NULL —
    deleting the parent must sever the reference, never destroy the child."""
    from ferro import ForeignKey

    class FkDriftConn(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        accounts: Relation[list["FkDriftAccount"]] = BackRef()

    class FkDriftAccount(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        connection: Annotated[
            FkDriftConn | None, ForeignKey(related_name="accounts")
        ] = None

    # Phase A: the pre-upgrade release creates the schema (default CASCADE)
    # and populates it.
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        parent = await FkDriftConn.create(name="c1")
        await FkDriftAccount.create(connection=parent)
    ferro.reset_engine()

    from ferro import clear_registry
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    # Phase B: identical models except the FK now declares SET NULL.
    class FkDriftConn(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        accounts: Relation[list["FkDriftAccount"]] = BackRef()

    class FkDriftAccount(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        connection: Annotated[
            FkDriftConn | None,
            ForeignKey(related_name="accounts", on_delete="SET NULL"),
        ] = None

    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():
        parents = await FkDriftConn.all()
        await parents[0].delete()

        # Sever, never destroy: the child survives with a nulled reference.
        children = await FkDriftAccount.all()
        assert len(children) == 1
        assert children[0].connection_id is None


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_fk_on_delete_drift_warns_loudly(db_url, clean_registry):
    """Issue #325 on SQLite: FK constraints cannot be altered in place, so an
    on_delete change on an existing table must warn loudly (naming the
    constraint) instead of diverging silently."""
    from ferro import ForeignKey

    class FkWarnConn(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        accounts: Relation[list["FkWarnAccount"]] = BackRef()

    class FkWarnAccount(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        connection: Annotated[
            FkWarnConn | None, ForeignKey(related_name="accounts")
        ] = None

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        parent = await FkWarnConn.create(name="c1")
        await FkWarnAccount.create(connection=parent)
    ferro.reset_engine()

    from ferro import clear_registry
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    class FkWarnConn(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        accounts: Relation[list["FkWarnAccount"]] = BackRef()

    class FkWarnAccount(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        connection: Annotated[
            FkWarnConn | None,
            ForeignKey(related_name="accounts", on_delete="SET NULL"),
        ] = None

    with pytest.warns(UserWarning, match=r"on_delete|fk_fkwarnaccount"):
        await ferro.connect(db_url, migrate_updates=True)


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
    async with ferro.engines.session():
        names_after_first = _live_index_names(db_url, db_backend, "idxnoopmodel")
        assert "idx_idxnoopmodel_x_y" in names_after_first
    ferro.reset_engine()

    # Second connect — same model, same index already present.
    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():

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
    async with ferro.engines.session():
        names = _live_index_names(db_url, db_backend, "idxdropmodel")
        assert "idx_idxdropmodel_p_q" in names
    ferro.reset_engine()

    # Re-register model WITHOUT the composite index.
    from ferro import clear_registry
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    class IdxDropModel(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        p: int
        q: int

    # Non-destructive: index must remain.
    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():
        names_after_updates = _live_index_names(db_url, db_backend, "idxdropmodel")
        assert "idx_idxdropmodel_p_q" in names_after_updates, (
            "non-destructive pass must leave orphaned ferro index intact"
        )
    ferro.reset_engine()

    # Destructive: index must be dropped.
    await ferro.connect(db_url, migrate_destructive=True)
    async with ferro.engines.session():
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
    async with ferro.engines.session():
        await execute('CREATE INDEX "my_custom_idx" ON "idxusermodel" ("m")')

        names_initial = _live_index_names(db_url, db_backend, "idxusermodel")
        assert "idx_idxusermodel_m_n" in names_initial
        assert "my_custom_idx" in names_initial
    ferro.reset_engine()

    # Non-destructive pass: both indexes must survive.
    await ferro.connect(db_url, migrate_updates=True)
    async with ferro.engines.session():
        names_after_updates = _live_index_names(db_url, db_backend, "idxusermodel")
        assert "idx_idxusermodel_m_n" in names_after_updates
        assert "my_custom_idx" in names_after_updates, (
            "user index must survive non-destructive auto-migrate"
        )
    ferro.reset_engine()

    # Destructive pass: Ferro index still present (model still has it), user index still present.
    await ferro.connect(db_url, migrate_destructive=True)
    async with ferro.engines.session():
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
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

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
    from ferro.registry import REGISTRY

    clear_registry()
    REGISTRY.reset_for_test()

    class UuidDriftModel(Model):  # noqa: F811
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str

    with _warnings.catch_warnings(record=True) as caught_first:
        _warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)

    ferro.reset_engine()

    # Second migrate_updates pass — must be identical to the first (idempotent).
    clear_registry()
    REGISTRY.reset_for_test()

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
    async with ferro.engines.session():

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


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_db_check_reconnect_is_idempotent(db_url):
    """G6 (#176): a `db_check=True` column emits a Postgres CHECK via a
    post-create `ALTER TABLE ... ADD CONSTRAINT`. That ALTER used to be
    non-idempotent, so a second `connect(auto_migrate=True)` against an
    already-migrated schema failed with `constraint "ck_..." already exists`.

    The emission is now guarded by an idempotent DO-block, so re-connecting
    is a no-op. Connect twice against the same schema; the second call must
    succeed and the constraint must exist exactly once.
    """
    from enum import StrEnum

    from ferro import Field as FerroField
    from ferro import clear_registry, connect, reset_engine
    from ferro.raw import fetch_all
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    # Match this file's other reconnect tests: wiping the model registry
    # without also draining the pending-relations queue would leave import-time
    # relations from module-level models in other test files dangling, so the
    # connect() below would crash in resolve_relationships when their
    # now-unregistered source model is looked up. reset_for_test() clears both.
    REGISTRY.reset_for_test()

    class DocStatus(StrEnum):
        PENDING = "pending"
        APPROVED = "approved"

    class ReconnectDoc(Model):
        id: int | None = FerroField(default=None, primary_key=True)
        status: DocStatus = FerroField(db_type="text", db_check=True)

    # First connect creates the table + CHECK constraint.
    await connect(db_url, auto_migrate=True)
    # Second connect re-runs the create path against the existing schema —
    # this raised OperationalError before the idempotency fix.
    reset_engine()  # G4b: a second unnamed connect() now raises; simulate a fresh process
    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():

        rows = await fetch_all(
            "SELECT conname FROM pg_constraint WHERE conname = 'ck_reconnectdoc_status'"
        )
        assert len(rows) == 1, f"expected exactly one CHECK constraint, got: {rows}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_failed_migration_rolls_back_whole_table_plan(db_url, clean_registry):
    """FF-G G3: a mid-plan DDL failure on Postgres leaves the table exactly
    as it was — earlier statements of the same table's plan are rolled back.

    The differ plans all AddColumn ops before AlterColumnType ops, so the
    ADD COLUMN for `added` executes first and the USING cast on `amount`
    (varchar 'not-a-number' → integer) fails second."""

    class MigTxRollback(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        added: str | None = None
        amount: int | None = None

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "migtxrollback" ("id" serial PRIMARY KEY, "amount" varchar)'
        )
        await execute(
            'INSERT INTO "migtxrollback" ("amount") VALUES (\'not-a-number\')'
        )

        with pytest.raises(Exception, match="Auto-migrate DDL failed"):
            await ferro.migrate()

        cols = await fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'migtxrollback'"
        )
        by_name = {c["column_name"]: c["data_type"] for c in cols}
        assert "added" not in by_name, "ADD COLUMN must be rolled back"
        assert by_name["amount"] == "character varying", "failed cast must not commit"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_jsonb_column_no_phantom_diff_on_reconnect(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """A jsonb-declared column reads back as jsonb — reconnect is quiet (#263).

    Before ADR-0004, introspection collapsed live jsonb to json, so every
    reconnect proposed a phantom ALTER back toward the declared type.
    """
    import warnings

    class JsonbEvent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        payload: Annotated[dict, FerroField(db_type="jsonb")]

    await ferro.connect(db_url, migrate_updates=True)  # creates payload -> jsonb
    ferro.reset_engine()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await ferro.connect(db_url, migrate_updates=True)  # must be quiet
    drift = [str(w.message) for w in caught if "json" in str(w.message).lower()]
    assert not drift, f"unexpected jsonb drift warning(s): {drift}"

    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "jsonbevent", "payload")
        == "jsonb"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_json_to_jsonb_declaration_edit_alters_in_place(
    db_url, postgres_base_url, db_schema_name, clean_registry
):
    """A live json column + jsonb declaration = one executed ALTER with the
    USING cast; existing row values survive the storage change (#263)."""

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "jsonbmigrated" '
            '("id" serial PRIMARY KEY, "payload" json)'
        )
        await execute(
            'INSERT INTO "jsonbmigrated" ("payload") VALUES (\'{"kept": true}\')'
        )
    ferro.reset_engine()

    class JsonbMigrated(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        payload: Annotated[dict | None, FerroField(db_type="jsonb")] = None

    await ferro.connect(db_url, migrate_updates=True)

    assert (
        _pg_live_type(postgres_base_url, db_schema_name, "jsonbmigrated", "payload")
        == "jsonb"
    )
    async with ferro.engines.session():
        migrated = await JsonbMigrated.get(1)
    assert migrated.payload == {"kept": True}


# ---------------------------------------------------------------------------
# CREATE TABLE dependency order with a self-referential FK (issue #302)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_self_fk_does_not_evict_component_from_create_order(
    db_url, clean_registry
):
    """A self-referential FK is not a cycle — the table depends only on itself —
    so it must not knock its dependency component out of the CREATE TABLE
    topological order. Before the #302 fix, `zorderednode`'s self-loop made the
    whole component fall back to name order, and `aorderedref` (which sorts
    first but carries a required FK to it) failed on a fresh Postgres schema
    with `relation "zorderednode" does not exist`."""
    from typing import Optional

    from ferro import ForeignKey

    class ZOrderedNode(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        parent: Annotated[
            Optional["ZOrderedNode"], ForeignKey(related_name="children")
        ] = None
        children: Relation[list["ZOrderedNode"]] = BackRef()
        refs: Relation[list["AOrderedRef"]] = BackRef()

    class AOrderedRef(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        node: Annotated[ZOrderedNode, ForeignKey(related_name="refs")]

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        root = await ZOrderedNode.create()
        child = await ZOrderedNode.create(parent=root)
        ref = await AOrderedRef.create(node=child)

        fetched = await AOrderedRef.get(ref.id)
        assert fetched.node_id == child.id


# ---------------------------------------------------------------------------
# #514: SQLite emits the inline column CHECK and ADD COLUMN ... REFERENCES
# instead of warn-skipping them.
# ---------------------------------------------------------------------------


def _sqlite_query(db_url: str, sql: str) -> list[tuple]:
    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _sqlite_table_sql(db_url: str, table: str) -> str:
    rows = _sqlite_query(
        db_url, f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{table}'"
    )
    return rows[0][0]


def _rewind_registry() -> None:
    from ferro import clear_registry, reset_engine
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_create_carries_the_named_inline_db_check(
    db_url, clean_registry, recwarn
):
    from enum import StrEnum

    from ferro import CheckViolationError

    class OrderStatus(StrEnum):
        OPEN = "open"
        SHIPPED = "shipped"

    class CheckedOrder(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: Annotated[OrderStatus, FerroField(db_type="text", db_check=True)]

    await ferro.connect(db_url, auto_migrate=True)

    assert (
        '"status" text NOT NULL CONSTRAINT "ck_checkedorder_status" '
        "CHECK (\"status\" IN ('open', 'shipped'))"
    ) in _sqlite_table_sql(db_url, "checkedorder")
    assert not [w for w in recwarn if "ck_checkedorder_status" in str(w.message)]

    async with ferro.engines.session():
        await CheckedOrder.create(status=OrderStatus.OPEN)
        with pytest.raises(CheckViolationError):
            await execute('INSERT INTO "checkedorder" ("status") VALUES (\'lost\')')


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_migrate_updates_adds_a_db_check_column_with_its_inline_check(
    db_url, clean_registry, recwarn
):
    from enum import StrEnum

    from ferro import CheckViolationError

    class ParcelSize(StrEnum):
        SMALL = "small"
        LARGE = "large"

    class Parcel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Parcel.create(label="first")
    _rewind_registry()

    class Parcel(Model):  # noqa: F811 — the same table, one column wider
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        size: Annotated[
            ParcelSize | None, FerroField(db_type="text", db_check=True)
        ] = None

    recwarn.clear()
    await ferro.connect(db_url, migrate_updates=True)

    assert (
        '"size" text CONSTRAINT "ck_parcel_size" '
        "CHECK (\"size\" IN ('small', 'large'))"
    ) in _sqlite_table_sql(db_url, "parcel")
    assert not [w for w in recwarn if "ck_parcel_size" in str(w.message)]

    async with ferro.engines.session():
        await Parcel.create(label="second", size=ParcelSize.LARGE)
        with pytest.raises(CheckViolationError):
            await execute(
                'INSERT INTO "parcel" ("label", "size") VALUES (\'third\', \'huge\')'
            )

    # The live table now carries the check: a second boot plans nothing for it.
    _rewind_registry()

    class Parcel(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str
        size: Annotated[
            ParcelSize | None, FerroField(db_type="text", db_check=True)
        ] = None

    recwarn.clear()
    await ferro.connect(db_url, migrate_updates=True)
    assert not [w for w in recwarn if "ck_parcel_size" in str(w.message)]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_sqlite_migrate_updates_adds_a_nullable_fk_column_with_references(
    db_url, clean_registry, recwarn
):
    from ferro import ForeignKey

    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    class Book(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await Book.create(title="untethered")
    _rewind_registry()

    class Author(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        books: Relation[list["Book"]] = BackRef()

    class Book(Model):  # noqa: F811 — gains a nullable FK
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[
            Author | None, ForeignKey(related_name="books", on_delete="CASCADE")
        ] = None

    recwarn.clear()
    await ferro.connect(db_url, migrate_updates=True)

    assert (
        '"author_id" integer REFERENCES "author"("id") ON DELETE CASCADE'
        in _sqlite_table_sql(db_url, "book")
    )
    fks = _sqlite_query(db_url, 'PRAGMA foreign_key_list("book")')
    # (id, seq, table, from, to, on_update, on_delete, match)
    assert [(fk[2], fk[3], fk[4], fk[6]) for fk in fks] == [
        ("author", "author_id", "id", "CASCADE")
    ]
    assert not [w for w in recwarn if "FOREIGN KEY" in str(w.message)]

    async with ferro.engines.session():
        author = await Author.create(name="Ursula")
        tethered = await Book.create(title="tethered", author=author)
        await execute('DELETE FROM "author"')
        assert await Book.where(lambda book: book.id == tethered.id).first() is None

    # Reconciled: a second boot neither re-adds nor warns about the FK.
    _rewind_registry()

    class Author(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        books: Relation[list["Book"]] = BackRef()

    class Book(Model):  # noqa: F811
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[
            Author | None, ForeignKey(related_name="books", on_delete="CASCADE")
        ] = None

    recwarn.clear()
    await ferro.connect(db_url, migrate_updates=True)
    assert not [w for w in recwarn if "FOREIGN KEY" in str(w.message)]


# ---------------------------------------------------------------------------
# Validity flags (#515; ADR-0043, ADR-0044): a live constraint or index can
# exist and still not be trusted. Postgres-only behaviour; on SQLite every
# flag reads `true` and the second boot plans nothing new.
# ---------------------------------------------------------------------------

VF_CHECK = "ck_vfpost_title_set"
VF_FK = "fk_vfpost_author_id_vfauthor"
VF_UNIQUE = "uq_vfpost_slug"


def _define_validity_models():
    from typing import ClassVar

    from ferro import Check, ForeignKey

    class VfAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        posts: Relation[list["VfPost"]] = BackRef()

    class VfPost(Model):
        __ferro_checks__: ClassVar[tuple[Check, ...]] = (
            Check("title_set", lambda vfpost: vfpost.title != None),  # noqa: E711
        )

        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str | None = None
        slug: Annotated[str | None, FerroField(unique=True)] = None
        author: Annotated[VfAuthor | None, ForeignKey(related_name="posts")] = None

    return VfAuthor, VfPost


class _ReconcileStatements(logging.Handler):
    """Collect the DDL the reconciliation pass logs for one table, in order."""

    def __init__(self, table: str):
        super().__init__(level=logging.DEBUG)
        self.prefix = f"Ferro Engine: auto-migrate executing on '{table}': "
        self.statements: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.prefix):
            self.statements.append(message[len(self.prefix) :])


async def _connect_capturing(db_url: str, table: str) -> list[str]:
    """``connect(migrate_updates=True)`` with fresh models; return the
    statements the pass executed for ``table``."""
    _rewind_registry()
    _define_validity_models()
    logger = logging.getLogger("ferro")
    handler = _ReconcileStatements(table)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await ferro.connect(db_url, migrate_updates=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return handler.statements


async def _pg_constraint(name: str) -> dict:
    rows = await fetch_all(
        "SELECT oid::bigint AS oid, convalidated, "
        "pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        f"WHERE conrelid = '\"vfpost\"'::regclass AND conname = '{name}'"
    )
    assert len(rows) == 1, f"expected exactly one {name}, got {rows}"
    return rows[0]


async def _pg_reinstall_not_valid(name: str) -> int:
    """Replace a ferro-installed constraint with the same definition added
    ``NOT VALID`` by hand. Returns the new constraint's oid."""
    definition = (await _pg_constraint(name))["definition"]
    await execute(f'ALTER TABLE "vfpost" DROP CONSTRAINT "{name}"')
    await execute(
        f'ALTER TABLE "vfpost" ADD CONSTRAINT "{name}" {definition} NOT VALID'
    )
    reinstalled = await _pg_constraint(name)
    assert reinstalled["convalidated"] is False
    assert reinstalled["definition"].endswith("NOT VALID")
    return reinstalled["oid"]


async def _bootstrap_validity_tables(db_url: str) -> None:
    _rewind_registry()
    _define_validity_models()
    await ferro.connect(db_url, auto_migrate=True)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("artifact", [VF_CHECK, VF_FK])
async def test_migrate_updates_validates_a_not_valid_constraint_in_place(
    db_url, db_backend, artifact, clean_registry
):
    await _bootstrap_validity_tables(db_url)
    oid = None
    if db_backend == "postgres":
        async with ferro.engines.session():
            oid = await _pg_reinstall_not_valid(artifact)

    executed = await _connect_capturing(db_url, "vfpost")
    if db_backend == "postgres":
        assert executed == [f'ALTER TABLE "vfpost" VALIDATE CONSTRAINT "{artifact}"']
        async with ferro.engines.session():
            after = await _pg_constraint(artifact)
        assert after["convalidated"] is True
        assert after["oid"] == oid, "validated in place, never dropped and re-added"
        assert not after["definition"].endswith("NOT VALID")
    else:
        assert executed == []

    assert await _connect_capturing(db_url, "vfpost") == []


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_rebuilds_an_invalid_index(
    db_url, db_backend, clean_registry
):
    await _bootstrap_validity_tables(db_url)
    if db_backend == "postgres":
        async with ferro.engines.session():
            await execute(
                "UPDATE pg_index SET indisvalid = false "
                f"WHERE indexrelid = '\"{VF_UNIQUE}\"'::regclass"
            )

    executed = await _connect_capturing(db_url, "vfpost")
    if db_backend == "postgres":
        assert executed == [
            f'DROP INDEX "{VF_UNIQUE}"',
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{VF_UNIQUE}" ON "vfpost" ("slug")',
        ]
        async with ferro.engines.session():
            rows = await fetch_all(
                "SELECT indisvalid FROM pg_index "
                f"WHERE indexrelid = '\"{VF_UNIQUE}\"'::regclass"
            )
        assert [row["indisvalid"] for row in rows] == [True]
    else:
        assert executed == []

    assert await _connect_capturing(db_url, "vfpost") == []


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_validate_of_a_check_with_violating_rows_raises_check_violation(
    db_url, clean_registry
):
    from ferro import CheckViolationError

    await _bootstrap_validity_tables(db_url)
    async with ferro.engines.session():
        await execute(f'ALTER TABLE "vfpost" DROP CONSTRAINT "{VF_CHECK}"')
        await execute('INSERT INTO "vfpost" ("title") VALUES (NULL)')
        await execute(
            f'ALTER TABLE "vfpost" ADD CONSTRAINT "{VF_CHECK}" '
            "CHECK (title IS NOT NULL) NOT VALID"
        )

    with pytest.raises(CheckViolationError) as excinfo:
        await _connect_capturing(db_url, "vfpost")
    assert excinfo.value.constraint == VF_CHECK

    # The failed validate rolled back with its table's plan: still NOT VALID.
    await ferro.connect(db_url)
    async with ferro.engines.session():
        assert (await _pg_constraint(VF_CHECK))["convalidated"] is False


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_validate_of_an_fk_with_violating_rows_raises_foreign_key_violation(
    db_url, clean_registry
):
    from ferro import ForeignKeyViolationError

    await _bootstrap_validity_tables(db_url)
    async with ferro.engines.session():
        definition = (await _pg_constraint(VF_FK))["definition"]
        await execute(f'ALTER TABLE "vfpost" DROP CONSTRAINT "{VF_FK}"')
        await execute('INSERT INTO "vfpost" ("title", "author_id") VALUES (\'t\', 999)')
        await execute(
            f'ALTER TABLE "vfpost" ADD CONSTRAINT "{VF_FK}" {definition} NOT VALID'
        )

    with pytest.raises(ForeignKeyViolationError) as excinfo:
        await _connect_capturing(db_url, "vfpost")
    assert excinfo.value.constraint == VF_FK

    # The failed validate rolled back with its table's plan: still NOT VALID.
    await ferro.connect(db_url)
    async with ferro.engines.session():
        assert (await _pg_constraint(VF_FK))["convalidated"] is False


# ---------------------------------------------------------------------------
# A declared rename in the reconciliation pass (#528, ADR-0032)
# ---------------------------------------------------------------------------


class _FerroDebug(logging.Handler):
    """Every ``ferro`` debug line, for counting the pass's table units."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _connect_logging(db_url: str) -> list[str]:
    logger = logging.getLogger("ferro")
    handler = _FerroDebug()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await ferro.connect(db_url, migrate_updates=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return handler.messages


def _define_hinted_pass_writer() -> None:
    from enum import StrEnum

    class PassGenre(StrEnum):
        NOVEL = "novel"
        POEM = "poem"

    class PassWriter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        full_name: Annotated[str, FerroField(index=True, renamed_from="name")]
        genre: Annotated[
            PassGenre,
            FerroField(db_type="text", db_check=True, renamed_from="kind"),
        ] = PassGenre.NOVEL


def _rewind() -> None:
    from ferro import clear_registry
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_renames_a_hinted_column_with_its_index_and_check_in_one_unit(
    db_url, db_backend, clean_registry
):
    from enum import StrEnum

    class PassGenre(StrEnum):
        NOVEL = "novel"
        POEM = "poem"

    class PassWriter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]
        kind: Annotated[PassGenre, FerroField(db_type="text", db_check=True)] = (
            PassGenre.NOVEL
        )

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute(
            'INSERT INTO "passwriter" ("name", "kind") VALUES (\'Ann\', \'poem\')'
        )
    _rewind()
    _define_hinted_pass_writer()

    if db_backend == "sqlite":
        # SQLite cannot rename a constraint in place; only a generated
        # migration's rebuild can.
        with pytest.warns(UserWarning, match=r"ck_passwriter_kind.*ferro migrate new"):
            messages = await _connect_logging(db_url)
    else:
        messages = await _connect_logging(db_url)

    # One table, one unit: both column renames, the index rename and (on
    # Postgres) the check rename ran together.
    units = [m for m in messages if m.startswith("✅ Ferro Engine: Table 'passwriter'")]
    assert len(units) == 1, units
    assert "(4 statement(s)" in units[0], units
    async with ferro.engines.session():
        rows = await fetch_all('SELECT "full_name", "genre" FROM "passwriter"')
        assert [(r["full_name"], r["genre"]) for r in rows] == [("Ann", "poem")]
        if db_backend == "postgres":
            checks = await fetch_all(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = '\"passwriter\"'::regclass AND contype = 'c'"
            )
            assert [r["conname"] for r in checks] == ["ck_passwriter_genre"]
    names = _live_index_names(db_url, db_backend, "passwriter")
    assert "idx_passwriter_full_name" in names and "idx_passwriter_name" not in names

    # The hint is inert once the live table holds the new names.
    _rewind()
    _define_hinted_pass_writer()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        again = await _connect_logging(db_url)
    assert not [m for m in again if m.startswith("✅ Ferro Engine: Table 'passwriter'")]


# ---------------------------------------------------------------------------
# A declared table rename in the pass (#528 follow-up, ADR-0032)
#
#     class TrnAuthor(Model):
#         __ferro_renamed_from__ = "trnwriter"
#
# against a database holding a populated "trnwriter" and no "trnauthor" is a
# rename, never a new empty "trnauthor" beside the old table.
# ---------------------------------------------------------------------------


def _define_trn_writer() -> None:
    class TrnWriter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]


def _define_trn_author() -> None:
    class TrnAuthor(Model):
        __ferro_renamed_from__ = "trnwriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]


async def _trn_writer_with_rows(db_url: str) -> None:
    _define_trn_writer()
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute("INSERT INTO \"trnwriter\" (\"name\") VALUES ('Ann'), ('Bo')")
    _rewind()


async def _trn_tables(db_url: str) -> set[str]:
    await ferro.connect(db_url)
    async with ferro.engines.session():
        if db_url.startswith("sqlite"):
            rows = await fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            names = {r["name"] for r in rows}
        else:
            rows = await fetch_all(
                "SELECT table_name::text AS name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            names = {r["name"] for r in rows}
    ferro.reset_engine()
    return {name for name in names if name.startswith("trn")}


async def _connect_capturing_logs(db_url: str, **flags) -> list[str]:
    logger = logging.getLogger("ferro")
    handler = _FerroDebug()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await ferro.connect(db_url, **flags)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return handler.messages


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_renames_a_hinted_table_with_its_rows_and_index(
    db_url, db_backend, clean_registry
):
    import warnings

    await _trn_writer_with_rows(db_url)
    _define_trn_author()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        messages = await _connect_capturing_logs(db_url, migrate_updates=True)
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]

    # The create pass created nothing: the table is the old one, renamed.
    assert not [m for m in messages if m.endswith("Table 'trnauthor' created")]
    assert [
        m
        for m in messages
        if m.startswith("✅ Ferro Engine: Table 'trnauthor' migrated")
    ]
    async with ferro.engines.session():
        rows = await fetch_all('SELECT "name" FROM "trnauthor" ORDER BY "id"')
        assert [r["name"] for r in rows] == ["Ann", "Bo"]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnauthor"}
    names = _live_index_names(db_url, db_backend, "trnauthor")
    assert "idx_trnauthor_name" in names and "idx_trnwriter_name" not in names

    # Nothing is left to do: the hint is inert against the renamed table.
    _rewind()
    _define_trn_author()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        again = await _connect_capturing_logs(db_url, migrate_updates=True)
    assert not [m for m in again if m.startswith("✅ Ferro Engine: Table 'trnauthor'")]
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]

    # And no drift: the live database read the way `drift` reads it (with
    # the hinted old table) plans to nothing against the models, destructive.
    import json

    from ferro import _core
    from ferro.ir.compiler import compile_registry_schema_ir

    declared = json.dumps(compile_registry_schema_ir())
    live_json, facts_json = await _core._live_schema_ir(
        None, json.dumps(["trnauthor"]), declared
    )
    plan = json.loads(
        _core._plan_from_ir(
            live_json,
            declared,
            db_backend,
            json.dumps({"destructive": True}),
            False,
            facts_json,
        )
    )
    assert plan == {"operations": [], "warnings": [], "always_warnings": []}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_hinted_table_without_migrate_updates_is_left_alone_and_warns(
    db_url, clean_registry
):
    await _trn_writer_with_rows(db_url)
    _define_trn_author()

    with pytest.warns(
        UserWarning,
        match=(
            r'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter".*'
            r"migrate_updates=True.*ferro migrate new"
        ),
    ):
        messages = await _connect_capturing_logs(db_url, auto_migrate=True)
    assert not [m for m in messages if m.endswith("Table 'trnauthor' created")]
    ferro.reset_engine()
    # No empty twin: the old table stands, with its rows.
    assert await _trn_tables(db_url) == {"trnwriter"}
    await ferro.connect(db_url)
    async with ferro.engines.session():
        rows = await fetch_all('SELECT "name" FROM "trnwriter" ORDER BY "id"')
        assert [r["name"] for r in rows] == ["Ann", "Bo"]

        # create_tables() is the same create pass, with the same word.
        with pytest.warns(
            UserWarning,
            match=(
                r'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter".*'
                r"migrate_updates=True.*ferro migrate new"
            ),
        ):
            await ferro.create_tables()
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_hinted_table_whose_new_name_is_also_live_is_inert(
    db_url, clean_registry
):
    import warnings

    await _trn_writer_with_rows(db_url)
    _define_trn_writer()

    class TrnAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]

    await ferro.connect(db_url, auto_migrate=True)
    _rewind()
    _define_trn_author()

    # Both names live: the hint is not live (ADR-0032), so it is inert and
    # silent, and the old table is no business of this modelset.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        messages = await _connect_capturing_logs(
            db_url, migrate_updates=True, migrate_destructive=True
        )
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]
    assert not [m for m in messages if m.startswith("✅ Ferro Engine: Table 'trn")]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter", "trnauthor"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_two_tables_claiming_one_live_old_name_refuse_and_change_nothing(
    db_url, clean_registry
):
    await _trn_writer_with_rows(db_url)

    class TrnAuthor(Model):
        __ferro_renamed_from__ = "trnwriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    class TrnPoet(Model):
        __ferro_renamed_from__ = "trnwriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    refusal = (
        r'rename hint refused: tables "trnauthor" and "trnpoet" all declare '
        r'__ferro_renamed_from__ = "trnwriter"'
    )
    for flags in (
        {"auto_migrate": True},
        {"migrate_updates": True, "migrate_destructive": True},
    ):
        with pytest.warns(UserWarning, match=refusal):
            messages = await _connect_capturing_logs(db_url, **flags)
        assert not [m for m in messages if m.startswith("✅ Ferro Engine: Table 'trn")]
        ferro.reset_engine()
        assert await _trn_tables(db_url) == {"trnwriter"}


def _define_trn_author_with_books() -> None:
    from ferro import ForeignKey

    class TrnAuthor(Model):
        __ferro_renamed_from__ = "trnwriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]
        books: Relation[list["TrnBook"]] = BackRef()

    class TrnBook(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated[TrnAuthor, ForeignKey("books")]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_new_table_referencing_a_renamed_one_is_created_after_the_rename(
    db_url, clean_registry
):
    await _trn_writer_with_rows(db_url)
    _define_trn_author_with_books()

    # Without migrate_updates neither is created: "trnbook" can only
    # reference "trnauthor" once the rename has run.
    with pytest.warns(
        UserWarning,
        match=r'"trnauthor" was not created, nor "trnbook", which reference it',
    ):
        await ferro.connect(db_url, auto_migrate=True)
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter"}

    _rewind()
    _define_trn_author_with_books()
    messages = await _connect_capturing_logs(db_url, migrate_updates=True)
    assert not [m for m in messages if m.endswith("' created")]
    async with ferro.engines.session():
        await execute(
            'INSERT INTO "trnbook" ("title", "author_id") VALUES (\'Odes\', 2)'
        )
        rows = await fetch_all(
            'SELECT "trnauthor"."name" FROM "trnbook" '
            'JOIN "trnauthor" ON "trnauthor"."id" = "trnbook"."author_id"'
        )
        assert [r["name"] for r in rows] == ["Bo"]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnauthor", "trnbook"}
