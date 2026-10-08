import re
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import Field

import ferro
from ferro import BackRef, ManyToMany, Model, PassReport, Relation
from ferro.base import FerroField
from tests._pass_harness import (
    auto_migrate,
    on,
    schema_steps,
    warned_auto_migrate,
    warning_texts,
)

pytestmark = pytest.mark.backend_matrix


def pg_sequence_rename(table: str, column: str = "id") -> str:
    """The pass's statement that gives ``table``'s ``column`` sequence the
    name a table created as ``table`` owns (``<table>_<column>_seq``) after a
    rename: ``ALTER TABLE … RENAME`` alone keeps the old name."""
    target = f"{table}_{column}_seq"
    return (
        f"DO $$ DECLARE seq regclass := pg_get_serial_sequence('\"{table}\"', "
        f"'{column}')::regclass; BEGIN IF seq IS NOT NULL AND (SELECT relname FROM "
        f"pg_class WHERE oid = seq) <> '{target}' THEN EXECUTE format('ALTER SEQUENCE "
        f"%s RENAME TO %I', seq, '{target}'); END IF; END $$"
    )


def sqlite_not_null_add(table: str, column: str, value: str) -> str:
    """The pass's report for a required column it adds on SQLite: nullable,
    backfilled by ``UPDATE``, the ``NOT NULL`` left to a rebuild, since
    SQLite's ``ADD COLUMN … NOT NULL`` keeps its ``DEFAULT`` for good and
    ferro persists none (ADR-0027)."""
    return (
        f"Column '{table}.{column}' was added nullable and its existing rows set to "
        f"{value}: SQLite adds a NOT NULL column only with a DEFAULT it keeps for good, "
        "and the model declares no server default. Generate a reviewed migration "
        "with `ferro migrate new` to rebuild the table with the column NOT NULL."
    )


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
    report = await auto_migrate(db_url)
    # In a full run the registry may hold other modules' models too: this
    # test's own table is what it asserts.
    assert [s for s in schema_steps(report) if s[0] == "automigrateduser"] == on(
        db_url,
        sqlite=[
            (
                "automigrateduser",
                'CREATE TABLE IF NOT EXISTS "automigrateduser" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "username" varchar NOT NULL )',
            ),
        ],
        postgres=[
            (
                "automigrateduser",
                'CREATE TABLE IF NOT EXISTS "automigrateduser" ( "id" serial PRIMARY KEY NOT NULL, "username" varchar NOT NULL )',
            ),
        ],
    )
    assert not [w for w in warning_texts(report) if "automigrateduser" in w]
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
        report = await ferro.create_tables()
        assert [s for s in schema_steps(report) if s[0] == "automigrateduser"] == on(
            db_url,
            sqlite=[
                (
                    "automigrateduser",
                    'CREATE TABLE IF NOT EXISTS "automigrateduser" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "username" varchar NOT NULL )',
                ),
            ],
            postgres=[
                (
                    "automigrateduser",
                    'CREATE TABLE IF NOT EXISTS "automigrateduser" ( "id" serial PRIMARY KEY NOT NULL, "username" varchar NOT NULL )',
                ),
            ],
        )
        assert not [w for w in warning_texts(report) if "automigrateduser" in w]
        assert True


@pytest.mark.asyncio
async def test_m2m_join_table_created_during_auto_migrate(db_url):
    """Verify that the many-to-many join table is created when auto_migrate=True.
    We clear registries, migrate a fresh in-memory DB, then use the M2M API; if the
    join table were not created, .add() would fail. No second connection needed."""
    from ferro import clear_registry, reset_engine
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "actor",
                'CREATE TABLE IF NOT EXISTS "actor" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "movie",
                'CREATE TABLE IF NOT EXISTS "movie" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL )',
            ),
            (
                "actor_movies",
                'CREATE TABLE IF NOT EXISTS "actor_movies" ( "actor_id" integer NOT NULL, "movie_id" integer NOT NULL, CONSTRAINT "fk_actor_movies_actor_id_actor" FOREIGN KEY ("actor_id") REFERENCES "actor" ("id") ON DELETE CASCADE, CONSTRAINT "fk_actor_movies_movie_id_movie" FOREIGN KEY ("movie_id") REFERENCES "movie" ("id") ON DELETE CASCADE )',
            ),
            (
                "actor_movies",
                'CREATE INDEX IF NOT EXISTS "idx_actor_movies_movie_id_actor_id" ON "actor_movies" ("movie_id", "actor_id")',
            ),
            (
                "actor_movies",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_actor_movies_actor_id_movie_id" ON "actor_movies" ("actor_id", "movie_id")',
            ),
        ],
        postgres=[
            (
                "actor",
                'CREATE TABLE IF NOT EXISTS "actor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "movie",
                'CREATE TABLE IF NOT EXISTS "movie" ( "id" serial PRIMARY KEY NOT NULL, "title" varchar NOT NULL )',
            ),
            (
                "actor_movies",
                'CREATE TABLE IF NOT EXISTS "actor_movies" ( "actor_id" integer NOT NULL, "movie_id" integer NOT NULL, CONSTRAINT "fk_actor_movies_actor_id_actor" FOREIGN KEY ("actor_id") REFERENCES "actor" ("id") ON DELETE CASCADE, CONSTRAINT "fk_actor_movies_movie_id_movie" FOREIGN KEY ("movie_id") REFERENCES "movie" ("id") ON DELETE CASCADE )',
            ),
            (
                "actor_movies",
                'CREATE INDEX IF NOT EXISTS "idx_actor_movies_movie_id_actor_id" ON "actor_movies" ("movie_id", "actor_id")',
            ),
            (
                "actor_movies",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_actor_movies_actor_id_movie_id" ON "actor_movies" ("actor_id", "movie_id")',
            ),
        ],
    )
    assert warning_texts(report) == []
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
    from ferro import clear_registry, reset_engine
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "uuidactor",
            'CREATE TABLE IF NOT EXISTS "uuidactor" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "name" varchar NOT NULL )',
        ),
        (
            "uuidmovie",
            'CREATE TABLE IF NOT EXISTS "uuidmovie" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "title" varchar NOT NULL )',
        ),
        (
            "uuidactor_movies",
            'CREATE TABLE IF NOT EXISTS "uuidactor_movies" ( "uuidactor_id" CHAR(32) NOT NULL, "uuidmovie_id" CHAR(32) NOT NULL, CONSTRAINT "fk_uuidactor_movies_uuidactor_id_uuidactor" FOREIGN KEY ("uuidactor_id") REFERENCES "uuidactor" ("id") ON DELETE CASCADE, CONSTRAINT "fk_uuidactor_movies_uuidmovie_id_uuidmovie" FOREIGN KEY ("uuidmovie_id") REFERENCES "uuidmovie" ("id") ON DELETE CASCADE )',
        ),
        (
            "uuidactor_movies",
            'CREATE INDEX IF NOT EXISTS "idx_uuidactor_movies_uuidmovie_id_uuidactor_id" ON "uuidactor_movies" ("uuidmovie_id", "uuidactor_id")',
        ),
        (
            "uuidactor_movies",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_uuidactor_movies_uuidactor_id_uuidmovie_id" ON "uuidactor_movies" ("uuidactor_id", "uuidmovie_id")',
        ),
    ]
    assert warning_texts(report) == []
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
    from ferro import clear_registry, reset_engine
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "uuidpost",
                'CREATE TABLE IF NOT EXISTS "uuidpost" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "title" varchar NOT NULL )',
            ),
            (
                "uuidtag",
                'CREATE TABLE IF NOT EXISTS "uuidtag" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "name" varchar NOT NULL )',
            ),
            (
                "uuidpost_tags",
                'CREATE TABLE IF NOT EXISTS "uuidpost_tags" ( "uuidpost_id" CHAR(32) NOT NULL, "uuidtag_id" CHAR(32) NOT NULL, CONSTRAINT "fk_uuidpost_tags_uuidpost_id_uuidpost" FOREIGN KEY ("uuidpost_id") REFERENCES "uuidpost" ("id") ON DELETE CASCADE, CONSTRAINT "fk_uuidpost_tags_uuidtag_id_uuidtag" FOREIGN KEY ("uuidtag_id") REFERENCES "uuidtag" ("id") ON DELETE CASCADE )',
            ),
            (
                "uuidpost_tags",
                'CREATE INDEX IF NOT EXISTS "idx_uuidpost_tags_uuidtag_id_uuidpost_id" ON "uuidpost_tags" ("uuidtag_id", "uuidpost_id")',
            ),
            (
                "uuidpost_tags",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_uuidpost_tags_uuidpost_id_uuidtag_id" ON "uuidpost_tags" ("uuidpost_id", "uuidtag_id")',
            ),
        ],
        postgres=[
            (
                "uuidpost",
                'CREATE TABLE IF NOT EXISTS "uuidpost" ( "id" uuid PRIMARY KEY NOT NULL, "title" varchar NOT NULL )',
            ),
            (
                "uuidtag",
                'CREATE TABLE IF NOT EXISTS "uuidtag" ( "id" uuid PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "uuidpost_tags",
                'CREATE TABLE IF NOT EXISTS "uuidpost_tags" ( "uuidpost_id" uuid NOT NULL, "uuidtag_id" uuid NOT NULL, CONSTRAINT "fk_uuidpost_tags_uuidpost_id_uuidpost" FOREIGN KEY ("uuidpost_id") REFERENCES "uuidpost" ("id") ON DELETE CASCADE, CONSTRAINT "fk_uuidpost_tags_uuidtag_id_uuidtag" FOREIGN KEY ("uuidtag_id") REFERENCES "uuidtag" ("id") ON DELETE CASCADE )',
            ),
            (
                "uuidpost_tags",
                'CREATE INDEX IF NOT EXISTS "idx_uuidpost_tags_uuidtag_id_uuidpost_id" ON "uuidpost_tags" ("uuidtag_id", "uuidpost_id")',
            ),
            (
                "uuidpost_tags",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_uuidpost_tags_uuidpost_id_uuidtag_id" ON "uuidpost_tags" ("uuidpost_id", "uuidtag_id")',
            ),
        ],
    )
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("miginvoice", 'ALTER TABLE "miginvoice" ADD COLUMN "memo" varchar'),
            ("miginvoice", 'ALTER TABLE "miginvoice" ADD COLUMN "paid_date" DATE'),
        ],
        postgres=[
            ("miginvoice", 'ALTER TABLE "miginvoice" ADD COLUMN "memo" varchar'),
            ("miginvoice", 'ALTER TABLE "miginvoice" ADD COLUMN "paid_date" date'),
        ],
    )
    assert warning_texts(report) == []
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

        report = await ferro.migrate()
        assert schema_steps(report) == [
            ("migreport", 'ALTER TABLE "migreport" ADD COLUMN "summary" varchar'),
        ]
        assert warning_texts(report) == []

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

    with pytest.raises(ValueError, match=r"migstrict\.created_at") as raised:
        await auto_migrate(db_url, updates=True)


    assert schema_steps(raised.value.report) == []
    assert warning_texts(raised.value.report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Column 'migdrift.count' is declared 'varchar' in the database but the model expects 'integer'. SQLite cannot change column types in place; generate a reviewed migration with `ferro migrate new` to migrate this column.",
        ]

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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == [
            (
                "document",
                'CREATE TABLE IF NOT EXISTS "document" ( "data" blob NOT NULL, "id" CHAR(32) NOT NULL PRIMARY KEY, "name" varchar NOT NULL )',
            ),
        ]
        assert warning_texts(report) == []

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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Column 'attachment.payload' is declared 'text' in the database but the model expects 'blob'. SQLite cannot change column types in place; generate a reviewed migration with `ferro migrate new` to migrate this column.",
        ]

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

    report = await auto_migrate(db_url, updates=True)  # creates docpg -> bytea
    assert schema_steps(report) == [
        (
            "docpg",
            'CREATE TABLE IF NOT EXISTS "docpg" ( "data" bytea NOT NULL, "id" uuid PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
    ferro.reset_engine()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(db_url, updates=True)  # reconnect: must be quiet
        assert schema_steps(report) == []
        assert warning_texts(report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Column 'event.occurred_at' is 'timestamp' in the database but the model maps `datetime` to 'timestamptz'. Ferro will not auto-convert it — a timestamp/timestamptz cast reinterprets existing values under the connection's timezone and can silently shift your data. To keep the column as-is, annotate the field with db_type=\"timestamp\". To convert it intentionally, use a reviewed migration (Alembic) with an explicit source timezone.",
        ]

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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []

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

    report = await auto_migrate(db_url)  # Ferro creates it -> timestamptz
    assert schema_steps(report) == [
        (
            "event3",
            'CREATE TABLE IF NOT EXISTS "event3" ( "id" serial PRIMARY KEY NOT NULL, "occurred_at" timestamp with time zone NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
    async with ferro.engines.session():
        assert (
            _pg_live_type(postgres_base_url, db_schema_name, "event3", "occurred_at")
            == "timestamp with time zone"
        )
    ferro.reset_engine()

    with _warnings.catch_warnings():
        _warnings.simplefilter("error", UserWarning)
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []

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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    async with ferro.engines.session():
        rows = await fetch_all('SELECT * FROM "migslim"')
        assert "legacy_notes" in rows[0]
    ferro.reset_engine()

    report = await auto_migrate(db_url, destructive=True)
    assert schema_steps(report) == [
        ("migslim", 'ALTER TABLE "migslim" DROP COLUMN "legacy_notes"'),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, destructive=True)
    assert schema_steps(report) == [
        ("migidx", 'DROP INDEX IF EXISTS "idx_migidx_old_status"'),
        ("migidx", 'ALTER TABLE "migidx" DROP COLUMN "old_status"'),
    ]
    assert warning_texts(report) == []
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

    with pytest.raises(ValueError, match=r"miguq\.old_code.*UNIQUE.*ferro migrate new") as raised:
        await auto_migrate(db_url, destructive=True)


    assert schema_steps(raised.value.report) == []
    assert warning_texts(raised.value.report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("migindexed", 'ALTER TABLE "migindexed" ADD COLUMN "slug" varchar'),
        (
            "migindexed",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_migindexed_slug" ON "migindexed" ("slug")',
        ),
        ("migindexed", 'ALTER TABLE "migindexed" ADD COLUMN "status" varchar'),
        (
            "migindexed",
            'CREATE INDEX IF NOT EXISTS "idx_migindexed_status" ON "migindexed" ("status")',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "migpg",
            'ALTER TABLE "migpg" ADD COLUMN "status" varchar NOT NULL DEFAULT \'draft\'',
        ),
        ("migpg", 'ALTER TABLE "migpg" ALTER COLUMN "status" DROP DEFAULT'),
        (
            "migpg",
            'ALTER TABLE "migpg" ALTER COLUMN "total" TYPE bigint USING "total"::bigint',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("migturns", 'ALTER TABLE "migturns" ADD COLUMN "turns" JSON'),
            (
                "migturns",
                'UPDATE "migturns" SET "turns" = \'{}\' WHERE "turns" IS NULL',
            ),
        ],
        postgres=[
            (
                "migturns",
                'ALTER TABLE "migturns" ADD COLUMN "turns" jsonb NOT NULL DEFAULT \'{}\'::jsonb',
            ),
            ("migturns", 'ALTER TABLE "migturns" ALTER COLUMN "turns" DROP DEFAULT'),
        ],
    )
    assert warning_texts(report) == on(
        db_url, sqlite=[sqlite_not_null_add("migturns", "turns", "'{}'")], postgres=[]
    )
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
            assert dflt is None, "SQLite adds it default-free and backfills by UPDATE"


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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("migflags", 'ALTER TABLE "migflags" ADD COLUMN "flags" JSON'),
            (
                "migflags",
                'UPDATE "migflags" SET "flags" = \'{}\' WHERE "flags" IS NULL',
            ),
        ],
        postgres=[
            (
                "migflags",
                'ALTER TABLE "migflags" ADD COLUMN "flags" jsonb NOT NULL DEFAULT \'{}\'::jsonb',
            ),
            ("migflags", 'ALTER TABLE "migflags" ALTER COLUMN "flags" DROP DEFAULT'),
        ],
    )
    assert warning_texts(report) == on(
        db_url, sqlite=[sqlite_not_null_add("migflags", "flags", "'{}'")], postgres=[]
    )
    async with ferro.engines.session():
        rows = await MigFlags.all()
        assert rows[0].flags == {}


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_required_column_with_a_default_leaves_no_server_default_on_sqlite(
    db_url, clean_registry
):
    """SQLite has no ``ALTER COLUMN … DROP DEFAULT``, so the pass never adds a
    ``NOT NULL … DEFAULT`` column there: the column comes in nullable with no
    ``dflt_value``, the rows are backfilled, and the ``NOT NULL`` is reported
    with the rebuild that adds it (`ferro migrate new`). A table
    ``create_tables()`` builds holds no ``DEFAULT`` either (ADR-0027)."""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "migtier" '
            '("id" integer PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL)'
        )
        await execute('INSERT INTO "migtier" ("name") VALUES (\'alpha\')')
    ferro.reset_engine()

    class MigTier(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        name: str
        tier: str = "free"

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("migtier", 'ALTER TABLE "migtier" ADD COLUMN "tier" varchar'),
        ("migtier", 'UPDATE "migtier" SET "tier" = \'free\' WHERE "tier" IS NULL'),
    ]
    assert warning_texts(report) == [sqlite_not_null_add("migtier", "tier", "'free'")]
    tier = _sqlite_columns(db_url, "migtier")["tier"]
    assert tier[4] is None, "no server default"
    async with ferro.engines.session():
        rows = await MigTier.all()
        assert [row.tier for row in rows] == ["free"]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_json_factory_is_not_a_create_table_server_default(db_url, clean_registry):
    """Field defaults stay client-side on CREATE TABLE (#373)."""

    class MigFreshTurns(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        turns: dict[str, dict] = ferro.Field(default_factory=dict)

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "migfreshturns",
            'CREATE TABLE IF NOT EXISTS "migfreshturns" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "turns" JSON NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "idxcompmodel",
            'CREATE INDEX IF NOT EXISTS "idx_idxcompmodel_col_a_col_b" ON "idxcompmodel" ("col_a", "col_b")',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "idxsinglemodel",
            'CREATE INDEX IF NOT EXISTS "idx_idxsinglemodel_status" ON "idxsinglemodel" ("status")',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "provtxn",
                'CREATE TABLE IF NOT EXISTS "provtxn" ( "account_id" integer NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )',
            ),
        ],
        postgres=[
            (
                "provtxn",
                'CREATE TABLE IF NOT EXISTS "provtxn" ( "account_id" integer NOT NULL, "id" serial PRIMARY KEY NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("provtxn", 'ALTER TABLE "provtxn" ADD COLUMN "pending" integer'),
            (
                "provtxn",
                'UPDATE "provtxn" SET "pending" = FALSE WHERE "pending" IS NULL',
            ),
            (
                "provtxn",
                'ALTER TABLE "provtxn" ADD COLUMN "provider_transaction_id" varchar',
            ),
            (
                "provtxn",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_provtxn_account_id_provider_transaction_id" ON "provtxn" ("account_id", "provider_transaction_id")',
            ),
        ],
        postgres=[
            (
                "provtxn",
                'ALTER TABLE "provtxn" ADD COLUMN "pending" bool NOT NULL DEFAULT FALSE',
            ),
            ("provtxn", 'ALTER TABLE "provtxn" ALTER COLUMN "pending" DROP DEFAULT'),
            (
                "provtxn",
                'ALTER TABLE "provtxn" ADD COLUMN "provider_transaction_id" varchar',
            ),
            (
                "provtxn",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_provtxn_account_id_provider_transaction_id" ON "provtxn" ("account_id", "provider_transaction_id")',
            ),
        ],
    )
    assert warning_texts(report) == on(
        db_url, sqlite=[sqlite_not_null_add("provtxn", "pending", "FALSE")], postgres=[]
    )
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "fkdriftconn",
            'CREATE TABLE IF NOT EXISTS "fkdriftconn" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
        ),
        (
            "fkdriftaccount",
            'CREATE TABLE IF NOT EXISTS "fkdriftaccount" ( "connection_id" integer, "id" serial PRIMARY KEY NOT NULL, CONSTRAINT "fk_fkdriftaccount_connection_id_fkdriftconn" FOREIGN KEY ("connection_id") REFERENCES "fkdriftconn" ("id") ON DELETE CASCADE )',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "fkdriftaccount",
            'ALTER TABLE "fkdriftaccount" DROP CONSTRAINT "fk_fkdriftaccount_connection_id_fkdriftconn"',
        ),
        (
            "fkdriftaccount",
            'ALTER TABLE "fkdriftaccount" ADD CONSTRAINT "fk_fkdriftaccount_connection_id_fkdriftconn" FOREIGN KEY ("connection_id") REFERENCES "fkdriftconn" ("id") ON DELETE SET NULL',
        ),
    ]
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "fkwarnconn",
            'CREATE TABLE IF NOT EXISTS "fkwarnconn" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
        ),
        (
            "fkwarnaccount",
            'CREATE TABLE IF NOT EXISTS "fkwarnaccount" ( "connection_id" integer, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, CONSTRAINT "fk_fkwarnaccount_connection_id_fkwarnconn" FOREIGN KEY ("connection_id") REFERENCES "fkwarnconn" ("id") ON DELETE CASCADE )',
        ),
    ]
    assert warning_texts(report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Foreign key on 'fkwarnaccount.connection_id' declares on_delete SET NULL but the live constraint enforces CASCADE; SQLite cannot alter constraints in place, so the live behavior remains. Generate a reviewed migration with `ferro migrate new` to apply the declared action.",
        ]


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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "idxnoopmodel",
                'CREATE TABLE IF NOT EXISTS "idxnoopmodel" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "x" integer NOT NULL, "y" integer NOT NULL )',
            ),
            (
                "idxnoopmodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxnoopmodel_x_y" ON "idxnoopmodel" ("x", "y")',
            ),
        ],
        postgres=[
            (
                "idxnoopmodel",
                'CREATE TABLE IF NOT EXISTS "idxnoopmodel" ( "id" serial PRIMARY KEY NOT NULL, "x" integer NOT NULL, "y" integer NOT NULL )',
            ),
            (
                "idxnoopmodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxnoopmodel_x_y" ON "idxnoopmodel" ("x", "y")',
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        names_after_first = _live_index_names(db_url, db_backend, "idxnoopmodel")
        assert "idx_idxnoopmodel_x_y" in names_after_first
    ferro.reset_engine()

    # Second connect — same model, same index already present.
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "idxdropmodel",
                'CREATE TABLE IF NOT EXISTS "idxdropmodel" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "p" integer NOT NULL, "q" integer NOT NULL )',
            ),
            (
                "idxdropmodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxdropmodel_p_q" ON "idxdropmodel" ("p", "q")',
            ),
        ],
        postgres=[
            (
                "idxdropmodel",
                'CREATE TABLE IF NOT EXISTS "idxdropmodel" ( "id" serial PRIMARY KEY NOT NULL, "p" integer NOT NULL, "q" integer NOT NULL )',
            ),
            (
                "idxdropmodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxdropmodel_p_q" ON "idxdropmodel" ("p", "q")',
            ),
        ],
    )
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    async with ferro.engines.session():
        names_after_updates = _live_index_names(db_url, db_backend, "idxdropmodel")
        assert "idx_idxdropmodel_p_q" in names_after_updates, (
            "non-destructive pass must leave orphaned ferro index intact"
        )
    ferro.reset_engine()

    # Destructive: index must be dropped.
    report = await auto_migrate(db_url, destructive=True)
    assert schema_steps(report) == [
        ("idxdropmodel", 'DROP INDEX IF EXISTS "idx_idxdropmodel_p_q"'),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "idxusermodel",
                'CREATE TABLE IF NOT EXISTS "idxusermodel" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "m" integer NOT NULL, "n" integer NOT NULL )',
            ),
            (
                "idxusermodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxusermodel_m_n" ON "idxusermodel" ("m", "n")',
            ),
        ],
        postgres=[
            (
                "idxusermodel",
                'CREATE TABLE IF NOT EXISTS "idxusermodel" ( "id" serial PRIMARY KEY NOT NULL, "m" integer NOT NULL, "n" integer NOT NULL )',
            ),
            (
                "idxusermodel",
                'CREATE INDEX IF NOT EXISTS "idx_idxusermodel_m_n" ON "idxusermodel" ("m", "n")',
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute('CREATE INDEX "my_custom_idx" ON "idxusermodel" ("m")')

        names_initial = _live_index_names(db_url, db_backend, "idxusermodel")
        assert "idx_idxusermodel_m_n" in names_initial
        assert "my_custom_idx" in names_initial
    ferro.reset_engine()

    # Non-destructive pass: both indexes must survive.
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    async with ferro.engines.session():
        names_after_updates = _live_index_names(db_url, db_backend, "idxusermodel")
        assert "idx_idxusermodel_m_n" in names_after_updates
        assert "my_custom_idx" in names_after_updates, (
            "user index must survive non-destructive auto-migrate"
        )
    ferro.reset_engine()

    # Destructive pass: Ferro index still present (model still has it), user index still present.
    report = await auto_migrate(db_url, destructive=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "uuidpkitem",
                'CREATE TABLE IF NOT EXISTS "uuidpkitem" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "label" varchar NOT NULL )',
            ),
        ],
        postgres=[
            (
                "uuidpkitem",
                'CREATE TABLE IF NOT EXISTS "uuidpkitem" ( "id" uuid PRIMARY KEY NOT NULL, "label" varchar NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []

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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "uuiddriftmodel",
                'CREATE TABLE IF NOT EXISTS "uuiddriftmodel" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "name" varchar NOT NULL )',
            ),
        ],
        postgres=[
            (
                "uuiddriftmodel",
                'CREATE TABLE IF NOT EXISTS "uuiddriftmodel" ( "id" uuid PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []

    ferro.reset_engine()

    # Second migrate_updates pass — must be identical to the first (idempotent).
    clear_registry()
    REGISTRY.reset_for_test()

    class UuidDriftModel(Model):  # noqa: F811
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str

    with _warnings.catch_warnings(record=True) as caught_second:
        _warnings.simplefilter("always")
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []

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
        report = await ferro.create_tables()
        assert schema_steps(report) == on(
            db_url,
            sqlite=[
                (
                    "latemodel",
                    'CREATE TABLE IF NOT EXISTS "latemodel" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "label" varchar NOT NULL )',
                ),
            ],
            postgres=[
                (
                    "latemodel",
                    'CREATE TABLE IF NOT EXISTS "latemodel" ( "id" serial PRIMARY KEY NOT NULL, "label" varchar NOT NULL )',
                ),
            ],
        )
        assert warning_texts(report) == []

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
    from ferro import clear_registry, reset_engine
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
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "reconnectdoc",
            'CREATE TABLE IF NOT EXISTS "reconnectdoc" ( "id" serial PRIMARY KEY NOT NULL, "status" text NOT NULL )',
        ),
        (
            "reconnectdoc",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_reconnectdoc_status' AND conrelid = '\"reconnectdoc\"'::regclass) THEN ALTER TABLE \"reconnectdoc\" ADD CONSTRAINT \"ck_reconnectdoc_status\" CHECK (\"status\" IN ('pending', 'approved')); END IF; END $$",
        ),
    ]
    assert warning_texts(report) == []
    # Second connect re-runs the create path against the existing schema —
    # this raised OperationalError before the idempotency fix.
    reset_engine()  # G4b: a second unnamed connect() now raises; simulate a fresh process
    report = await auto_migrate(db_url)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    async with ferro.engines.session():

        rows = await fetch_all(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'ck_reconnectdoc_status' "
            "AND connamespace = current_schema()::regnamespace"
        )
        assert len(rows) == 1, f"expected exactly one CHECK constraint, got: {rows}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_a_pass_that_fails_after_committing_a_table_refreshes_the_pool(
    db_url, clean_registry
):
    """Each table's plan commits on its own: when the second table's fails,
    the first's ADD COLUMN stands, so the engine's pool is refreshed as
    after a pass that finished, and a query prepared before the pass does
    not run its stale plan against the wider table."""

    class AlphaWiden(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        summary: str | None = None

    class ZuluRetype(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: int | None = None

    await ferro.connect(db_url, name="app")
    await execute(
        'CREATE TABLE "alphawiden" ("id" serial PRIMARY KEY, "title" varchar NOT NULL)',
        using="app",
    )
    await execute('INSERT INTO "alphawiden" ("title") VALUES (\'Q1\')', using="app")
    await execute(
        'CREATE TABLE "zuluretype" ("id" serial PRIMARY KEY, "amount" varchar)',
        using="app",
    )
    await execute(
        'INSERT INTO "zuluretype" ("amount") VALUES (\'not-a-number\')', using="app"
    )
    # Prepare (and cache) the query on every connection of the pool.
    for _ in range(20):
        assert len(await fetch_all('SELECT * FROM "alphawiden"', using="app")) == 1

    with pytest.raises(Exception, match="Auto-migrate DDL failed") as raised:
        await ferro.migrate(using="app")

    assert schema_steps(raised.value.report) == [
        ("alphawiden", 'ALTER TABLE "alphawiden" ADD COLUMN "summary" varchar'),
    ]
    for _ in range(20):
        rows = await fetch_all('SELECT * FROM "alphawiden"', using="app")
        assert [row["summary"] for row in rows] == [None]


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

        with pytest.raises(Exception, match="Auto-migrate DDL failed") as raised:
            await ferro.migrate()

        # The table's plan (ADD COLUMN "added", then the failing ALTER COLUMN
        # "amount" TYPE integer) rolled back whole: the error's report lists
        # nothing committed, and the error names the failing statement.
        assert schema_steps(raised.value.report) == []
        assert warning_texts(raised.value.report) == []
        assert (
            'ALTER TABLE "migtxrollback" ALTER COLUMN "amount" TYPE integer '
            'USING "amount"::integer' in str(raised.value)
        )
        cols = await fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'migtxrollback'"
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

    report = await auto_migrate(db_url, updates=True)  # creates payload -> jsonb
    assert schema_steps(report) == [
        (
            "jsonbevent",
            'CREATE TABLE IF NOT EXISTS "jsonbevent" ( "id" serial PRIMARY KEY NOT NULL, "payload" jsonb NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
    ferro.reset_engine()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(db_url, updates=True)  # must be quiet
        assert schema_steps(report) == []
        assert warning_texts(report) == []
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

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "jsonbmigrated",
            'ALTER TABLE "jsonbmigrated" ALTER COLUMN "payload" TYPE jsonb USING "payload"::jsonb',
        ),
    ]
    assert warning_texts(report) == []

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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "zorderednode",
                'CREATE TABLE IF NOT EXISTS "zorderednode" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "parent_id" CHAR(32), CONSTRAINT "fk_zorderednode_parent_id_zorderednode" FOREIGN KEY ("parent_id") REFERENCES "zorderednode" ("id") ON DELETE CASCADE )',
            ),
            (
                "aorderedref",
                'CREATE TABLE IF NOT EXISTS "aorderedref" ( "id" CHAR(32) NOT NULL PRIMARY KEY, "node_id" CHAR(32) NOT NULL, CONSTRAINT "fk_aorderedref_node_id_zorderednode" FOREIGN KEY ("node_id") REFERENCES "zorderednode" ("id") ON DELETE CASCADE )',
            ),
        ],
        postgres=[
            (
                "zorderednode",
                'CREATE TABLE IF NOT EXISTS "zorderednode" ( "id" uuid PRIMARY KEY NOT NULL, "parent_id" uuid, CONSTRAINT "fk_zorderednode_parent_id_zorderednode" FOREIGN KEY ("parent_id") REFERENCES "zorderednode" ("id") ON DELETE CASCADE )',
            ),
            (
                "aorderedref",
                'CREATE TABLE IF NOT EXISTS "aorderedref" ( "id" uuid PRIMARY KEY NOT NULL, "node_id" uuid NOT NULL, CONSTRAINT "fk_aorderedref_node_id_zorderednode" FOREIGN KEY ("node_id") REFERENCES "zorderednode" ("id") ON DELETE CASCADE )',
            ),
        ],
    )
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "checkedorder",
            'CREATE TABLE IF NOT EXISTS "checkedorder" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" text NOT NULL CONSTRAINT "ck_checkedorder_status" CHECK ("status" IN (\'open\', \'shipped\')) )',
        ),
    ]
    assert warning_texts(report) == []

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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "parcel",
            'CREATE TABLE IF NOT EXISTS "parcel" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "label" varchar NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "parcel",
            'ALTER TABLE "parcel" ADD COLUMN "size" text CONSTRAINT "ck_parcel_size" CHECK ("size" IN (\'small\', \'large\'))',
        ),
    ]
    assert warning_texts(report) == []

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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "author",
            'CREATE TABLE IF NOT EXISTS "author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
        ),
        (
            "book",
            'CREATE TABLE IF NOT EXISTS "book" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "book",
            'ALTER TABLE "book" ADD COLUMN "author_id" integer REFERENCES "author"("id") ON DELETE CASCADE',
        ),
    ]
    assert warning_texts(report) == []

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
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
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


async def _connect_capturing(db_url: str) -> PassReport:
    """``connect(migrate_updates=True)`` with fresh models; the pass's report."""
    _rewind_registry()
    _define_validity_models()
    return await auto_migrate(db_url, updates=True)


def _table_sql(report: PassReport, table: str) -> list[str]:
    """The schema statements the pass executed for ``table``, in order."""
    return [sql for subject, sql in schema_steps(report) if subject == table]


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


async def _bootstrap_validity_tables(db_url: str) -> PassReport:
    _rewind_registry()
    _define_validity_models()
    return await auto_migrate(db_url)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize("artifact", [VF_CHECK, VF_FK])
async def test_migrate_updates_validates_a_not_valid_constraint_in_place(
    db_url, db_backend, artifact, clean_registry
):
    report = await _bootstrap_validity_tables(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "vfauthor",
                'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "vfpost",
                'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
            ),
            (
                "vfpost",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
            ),
        ],
        postgres=[
            (
                "vfauthor",
                'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "vfpost",
                'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" serial PRIMARY KEY NOT NULL, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
            ),
            (
                "vfpost",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
            ),
        ],
    )
    assert warning_texts(report) == []
    oid = None
    if db_backend == "postgres":
        async with ferro.engines.session():
            oid = await _pg_reinstall_not_valid(artifact)

    report = await _connect_capturing(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[],
        postgres=[("vfpost", f'ALTER TABLE "vfpost" VALIDATE CONSTRAINT "{artifact}"')],
    )
    assert warning_texts(report) == []
    executed = _table_sql(report, "vfpost")
    if db_backend == "postgres":
        assert executed == [f'ALTER TABLE "vfpost" VALIDATE CONSTRAINT "{artifact}"']
        async with ferro.engines.session():
            after = await _pg_constraint(artifact)
        assert after["convalidated"] is True
        assert after["oid"] == oid, "validated in place, never dropped and re-added"
        assert not after["definition"].endswith("NOT VALID")
    else:
        assert executed == []

    report = await _connect_capturing(db_url)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    assert _table_sql(report, "vfpost") == []


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_rebuilds_an_invalid_index(
    db_url, db_backend, clean_registry
):
    report = await _bootstrap_validity_tables(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "vfauthor",
                'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "vfpost",
                'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
            ),
            (
                "vfpost",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
            ),
        ],
        postgres=[
            (
                "vfauthor",
                'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "vfpost",
                'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" serial PRIMARY KEY NOT NULL, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
            ),
            (
                "vfpost",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
            ),
        ],
    )
    assert warning_texts(report) == []
    if db_backend == "postgres":
        async with ferro.engines.session():
            await execute(
                "UPDATE pg_index SET indisvalid = false "
                f"WHERE indexrelid = '\"{VF_UNIQUE}\"'::regclass"
            )

    report = await _connect_capturing(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[],
        postgres=[
            ("vfpost", 'DROP INDEX "uq_vfpost_slug"'),
            (
                "vfpost",
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
            ),
        ],
    )
    assert warning_texts(report) == []
    executed = _table_sql(report, "vfpost")
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

    report = await _connect_capturing(db_url)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    assert _table_sql(report, "vfpost") == []


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_validate_of_a_check_with_violating_rows_raises_check_violation(
    db_url, clean_registry
):
    from ferro import CheckViolationError

    report = await _bootstrap_validity_tables(db_url)
    assert schema_steps(report) == [
        (
            "vfauthor",
            'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
        ),
        (
            "vfpost",
            'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" serial PRIMARY KEY NOT NULL, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
        ),
        (
            "vfpost",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
        ),
    ]
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute(f'ALTER TABLE "vfpost" DROP CONSTRAINT "{VF_CHECK}"')
        await execute('INSERT INTO "vfpost" ("title") VALUES (NULL)')
        await execute(
            f'ALTER TABLE "vfpost" ADD CONSTRAINT "{VF_CHECK}" '
            "CHECK (title IS NOT NULL) NOT VALID"
        )

    with pytest.raises(CheckViolationError) as excinfo:
        await _connect_capturing(db_url)
    # The table's plan ran in one transaction and rolled back whole: the
    # report carried by the error lists nothing committed, and the error
    # names the statement that failed.
    assert excinfo.value.report == PassReport()
    assert f'ALTER TABLE "vfpost" VALIDATE CONSTRAINT "{VF_CHECK}"' in str(excinfo.value)
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

    report = await _bootstrap_validity_tables(db_url)
    assert schema_steps(report) == [
        (
            "vfauthor",
            'CREATE TABLE IF NOT EXISTS "vfauthor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
        ),
        (
            "vfpost",
            'CREATE TABLE IF NOT EXISTS "vfpost" ( "author_id" integer, "id" serial PRIMARY KEY NOT NULL, "slug" varchar, "title" varchar, CONSTRAINT "fk_vfpost_author_id_vfauthor" FOREIGN KEY ("author_id") REFERENCES "vfauthor" ("id") ON DELETE CASCADE, CONSTRAINT "ck_vfpost_title_set" CHECK ("title" IS NOT NULL) )',
        ),
        (
            "vfpost",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_vfpost_slug" ON "vfpost" ("slug")',
        ),
    ]
    assert warning_texts(report) == []
    async with ferro.engines.session():
        definition = (await _pg_constraint(VF_FK))["definition"]
        await execute(f'ALTER TABLE "vfpost" DROP CONSTRAINT "{VF_FK}"')
        await execute('INSERT INTO "vfpost" ("title", "author_id") VALUES (\'t\', 999)')
        await execute(
            f'ALTER TABLE "vfpost" ADD CONSTRAINT "{VF_FK}" {definition} NOT VALID'
        )

    with pytest.raises(ForeignKeyViolationError) as excinfo:
        await _connect_capturing(db_url)
    # The table's plan ran in one transaction and rolled back whole: the
    # report carried by the error lists nothing committed, and the error
    # names the statement that failed.
    assert excinfo.value.report == PassReport()
    assert f'ALTER TABLE "vfpost" VALIDATE CONSTRAINT "{VF_FK}"' in str(excinfo.value)
    assert excinfo.value.constraint == VF_FK

    # The failed validate rolled back with its table's plan: still NOT VALID.
    await ferro.connect(db_url)
    async with ferro.engines.session():
        assert (await _pg_constraint(VF_FK))["convalidated"] is False


# ---------------------------------------------------------------------------
# A declared rename in the reconciliation pass (#528, ADR-0032)
# ---------------------------------------------------------------------------


def _lock_timeout_units(report: PassReport) -> int:
    """How many transactional units the pass opened on Postgres: each
    starts with its own ``SET LOCAL lock_timeout`` (ADR-0044)."""
    return sum(
        1
        for s in report.statements
        if s.role == "lock_timeout" and s.sql.startswith("SET LOCAL")
    )


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

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "passwriter",
                'CREATE TABLE IF NOT EXISTS "passwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "kind" text NOT NULL CONSTRAINT "ck_passwriter_kind" CHECK ("kind" IN (\'novel\', \'poem\')), "name" varchar NOT NULL )',
            ),
            (
                "passwriter",
                'CREATE INDEX IF NOT EXISTS "idx_passwriter_name" ON "passwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "passwriter",
                'CREATE TABLE IF NOT EXISTS "passwriter" ( "id" serial PRIMARY KEY NOT NULL, "kind" text NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "passwriter",
                'CREATE INDEX IF NOT EXISTS "idx_passwriter_name" ON "passwriter" ("name")',
            ),
            (
                "passwriter",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_passwriter_kind' AND conrelid = '\"passwriter\"'::regclass) THEN ALTER TABLE \"passwriter\" ADD CONSTRAINT \"ck_passwriter_kind\" CHECK (\"kind\" IN ('novel', 'poem')); END IF; END $$",
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute(
            'INSERT INTO "passwriter" ("name", "kind") VALUES (\'Ann\', \'poem\')'
        )
    _rewind()
    _define_hinted_pass_writer()

    if db_backend == "sqlite":
        # SQLite cannot rename a constraint in place; only a generated
        # migration's rebuild can.
        report = await warned_auto_migrate(db_url, updates=True)
        assert schema_steps(report) == [
            ("passwriter", 'ALTER TABLE "passwriter" RENAME COLUMN "kind" TO "genre"'),
            ("passwriter", 'ALTER TABLE "passwriter" RENAME COLUMN "name" TO "full_name"'),
            ("passwriter", 'DROP INDEX IF EXISTS "idx_passwriter_name"'),
            (
                "passwriter",
                'CREATE INDEX IF NOT EXISTS "idx_passwriter_full_name" ON "passwriter" ("full_name")',
            ),
        ]
        assert warning_texts(report) == [
            "Table 'passwriter' has CHECK constraint(s) 'ck_passwriter_kind' that the model no longer declares. Leftover CHECKs keep rejecting rows the model now allows. They stay in place unless you pass migrate_destructive=True (Postgres) or drop them with a reviewed migration (`ferro migrate new`).",
            "Constraint 'ck_passwriter_kind' on 'passwriter' is now named 'ck_passwriter_genre', and SQLite cannot rename a table constraint in place; `ferro migrate new` renames it by rebuilding the table.",
            "Check constraint 'ck_passwriter_genre' on column 'passwriter.genre' is declared but missing from the live table, and SQLite cannot add a constraint to an existing column (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.",
        ]
    else:
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == [
            ("passwriter", 'ALTER TABLE "passwriter" RENAME COLUMN "kind" TO "genre"'),
            ("passwriter", 'ALTER TABLE "passwriter" RENAME COLUMN "name" TO "full_name"'),
            (
                "passwriter",
                'ALTER INDEX "idx_passwriter_name" RENAME TO "idx_passwriter_full_name"',
            ),
            (
                "passwriter",
                'ALTER TABLE "passwriter" RENAME CONSTRAINT "ck_passwriter_kind" TO "ck_passwriter_genre"',
            ),
        ]
        assert warning_texts(report) == []

    # One table, one unit: both column renames, the index rename and (on
    # Postgres) the check rename ran together, in one transaction there.
    executed = [sql for subject, sql in schema_steps(report) if subject == "passwriter"]
    assert len(executed) == 4, executed
    if db_backend == "postgres":
        assert _lock_timeout_units(report) == 1, report.statements
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == on(
            db_url,
            sqlite=[
                "Table 'passwriter' has CHECK constraint(s) 'ck_passwriter_kind' that the model no longer declares. Leftover CHECKs keep rejecting rows the model now allows. They stay in place unless you pass migrate_destructive=True (Postgres) or drop them with a reviewed migration (`ferro migrate new`).",
                "Check constraint 'ck_passwriter_genre' on column 'passwriter.genre' is declared but missing from the live table, and SQLite cannot add a constraint to an existing column (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.",
            ],
            postgres=[],
        )
    assert not [s for s in schema_steps(report) if s[0] == "passwriter"]


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_renames_a_hinted_column_and_then_changes_its_type(
    db_url, db_backend, clean_registry
):
    """``name: int`` becomes ``full_name: str`` with ``renamed_from="name"``:
    the pass renames the column, then changes its type under the new name
    (#538, F2) — never a refusal that the column is missing."""

    class Pf2Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: int

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "pf2author",
                'CREATE TABLE IF NOT EXISTS "pf2author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" integer NOT NULL )',
            ),
        ],
        postgres=[
            (
                "pf2author",
                'CREATE TABLE IF NOT EXISTS "pf2author" ( "id" serial PRIMARY KEY NOT NULL, "name" integer NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute('INSERT INTO "pf2author" ("name") VALUES (42)')
    _rewind()

    class Pf2Author(Model):  # noqa: F811 - the edited model
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        full_name: Annotated[str, FerroField(renamed_from="name")]

    if db_backend == "sqlite":
        # SQLite renames in place but cannot change a type in place: the
        # type change is the pass's usual warning (ADR-0014).
        with pytest.warns(
            UserWarning, match=r"pf2author\.full_name.*ferro migrate new"
        ):
            report = await auto_migrate(db_url, updates=True)
            assert schema_steps(report) == [
                ("pf2author", 'ALTER TABLE "pf2author" RENAME COLUMN "name" TO "full_name"'),
            ]
            assert warning_texts(report) == [
                "Column 'pf2author.full_name' is declared 'int' in the database but the model expects 'varchar'. SQLite cannot change column types in place; generate a reviewed migration with `ferro migrate new` to migrate this column.",
            ]
    else:
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == [
            ("pf2author", 'ALTER TABLE "pf2author" RENAME COLUMN "name" TO "full_name"'),
            (
                "pf2author",
                'ALTER TABLE "pf2author" ALTER COLUMN "full_name" TYPE varchar USING "full_name"::varchar',
            ),
        ]
        assert warning_texts(report) == []

    async with ferro.engines.session():
        rows = await fetch_all('SELECT "full_name" FROM "pf2author"')
        assert [str(r["full_name"]) for r in rows] == ["42"]
        if db_backend == "postgres":
            types = await fetch_all(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'pf2author' "
                "AND column_name IN ('name', 'full_name')"
            )
            assert [(r["column_name"], r["data_type"]) for r in types] == [
                ("full_name", "character varying")
            ]


# ---------------------------------------------------------------------------
# A declared label rename in the pass (#538, D3; ADR-0032, ADR-0014)
# ---------------------------------------------------------------------------


def _define_pd3_order(renamed: bool, checked: bool = False) -> None:
    """``Pd3Order.status`` of ``paid``/``canceled``; ``renamed``: of
    ``paid``/``cancelled`` with the hint ``{"cancelled": "canceled"}``.
    ``checked``: stored as text with its ``db_check``."""
    from enum import StrEnum

    if renamed:

        class Pd3Status(StrEnum):
            __ferro_renamed_labels__ = {"cancelled": "canceled"}
            PAID = "paid"
            CANCELLED = "cancelled"

    else:

        class Pd3Status(StrEnum):
            PAID = "paid"
            CANCELED = "canceled"

    if checked:

        class Pd3Order(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            status: Annotated[Pd3Status, FerroField(db_type="text", db_check=True)]

    else:

        class Pd3Order(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            status: Pd3Status


_PD3_STRANDED = (
    r"Column 'pd3order\.status' still holds the enum label 'canceled', which the "
    r"model now declares as 'cancelled' .*`ferro migrate new`"
)

# The pass's warning for a live label rename on SQLite, in full.
_PD3_STRANDED_TEXT = (
    "Column 'pd3order.status' still holds the enum label 'canceled', which the "
    "model now declares as 'cancelled' (__ferro_renamed_labels__). SQLite keeps "
    "enum labels as text in the rows, and auto-migrate changes the schema, never "
    "the rows: they keep 'canceled', which the model no longer reads or writes. "
    "Generate the migration that relabels them with `ferro migrate new`."
)

# The renderer's word on a checked column whose check body the label rename
# changed: SQLite cannot rebuild the check in place.
_PD3_CHECK_BODY_TEXT = (
    "CHECK constraint 'ck_pd3order_status' on table 'pd3order' has a declared "
    "body that differs from the live constraint, and SQLite cannot alter "
    "constraints in place (it requires a full table rebuild). The live body "
    "remains; generate a reviewed migration with `ferro migrate new` to apply the "
    "declared predicate."
)

_PD3_SQLITE_PLAIN = (
    'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY '
    'AUTOINCREMENT, "status" varchar(8) NOT NULL )'
)
_PD3_SQLITE_CHECKED = (
    'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY '
    'AUTOINCREMENT, "status" text NOT NULL CONSTRAINT "ck_pd3order_status" CHECK '
    "(\"status\" IN ('paid', 'canceled')) )"
)


async def _pd3_connect(db_url: str, *, updates: bool) -> PassReport:
    """The pass ``connect(auto_migrate=True)`` (or, with ``updates``,
    ``connect(migrate_updates=True)``) runs; its report, each warning it
    raised captured as the report's."""
    return await warned_auto_migrate(db_url, updates=updates)


def _stranded(report: PassReport) -> list[str]:
    """The label-rename warnings the pass raised."""
    stranded = [str(w) for w in report.warnings if w.kind == "StrandedLabelRename"]
    assert all(re.search(_PD3_STRANDED, text) for text in stranded), stranded
    return stranded


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [True, False], ids=["updates", "plain"])
async def test_a_label_rename_on_sqlite_warns_and_leaves_the_rows(
    db_url, db_backend, clean_registry, updates
):
    """SQLite keeps an enum's labels as text in its rows. The pass never
    rewrites rows (ADR-0014), so a live label rename (a row holds the old
    label) is one warning naming `ferro migrate new` under
    ``migrate_updates``; plain ``auto_migrate`` reconciles nothing and says
    nothing (ADR-0011). The rows stay as they are either way. On Postgres the
    native type renames its label in place, unchanged by this (#538, D3)."""
    _define_pd3_order(renamed=False)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "pd3order",
                'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" varchar(8) NOT NULL )',
            ),
        ],
        postgres=[
            (
                "pd3status",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typname = 'pd3status' AND n.nspname = current_schema()) THEN CREATE TYPE \"pd3status\" AS ENUM ('paid', 'canceled'); END IF; END $$",
            ),
            (
                "pd3order",
                'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" serial PRIMARY KEY NOT NULL, "status" pd3status NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute(
            "INSERT INTO \"pd3order\" (\"status\") VALUES ('canceled'), ('paid')"
        )
    _rewind()
    _define_pd3_order(renamed=True)

    report = await _pd3_connect(db_url, updates=updates)
    # Postgres renames the native type's label in place; SQLite changes no
    # schema and, under migrate_updates, warns about the rows.
    renames = [("pd3status", "ALTER TYPE \"pd3status\" RENAME VALUE 'canceled' TO 'cancelled'")]
    assert schema_steps(report) == on(
        db_url, sqlite=[], postgres=renames if updates else []
    )
    assert warning_texts(report) == on(
        db_url, sqlite=[_PD3_STRANDED_TEXT] if updates else [], postgres=[]
    )

    async with ferro.engines.session():
        rows = await fetch_all('SELECT "status" FROM "pd3order" ORDER BY "id"')
        statuses = [r["status"] for r in rows]
        if db_backend == "postgres":
            labels = await fetch_all(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname = 'pd3status' AND n.nspname = current_schema() "
                "ORDER BY e.enumsortorder"
            )
    if db_backend == "sqlite":
        stranded = 1 if updates else 0
        assert len(_stranded(report)) == stranded, report.warnings
        assert statuses == ["canceled", "paid"]
        return
    assert _stranded(report) == [], report.warnings
    if updates:
        assert statuses == ["cancelled", "paid"]
        assert [r["enumlabel"] for r in labels] == ["paid", "cancelled"]
    else:
        assert statuses == ["canceled", "paid"]


@pytest.mark.asyncio
@pytest.mark.sqlite_only
@pytest.mark.parametrize("held", [False, True], ids=["no-row", "a-row"])
@pytest.mark.parametrize("checked", [False, True], ids=["plain", "db_check"])
async def test_a_label_rename_on_sqlite_warns_only_while_the_database_holds_the_old_label(
    db_url, clean_registry, checked, held
):
    """The hint is live while a row holds the old label or the column's
    ``db_check`` still lists it (ADR-0032); otherwise it is inert and silent."""
    _define_pd3_order(renamed=False, checked=checked)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        ("pd3order", _PD3_SQLITE_CHECKED if checked else _PD3_SQLITE_PLAIN)
    ]
    assert warning_texts(report) == []
    values = "('canceled'), ('paid')" if held else "('paid')"
    async with ferro.engines.session():
        await execute(f'INSERT INTO "pd3order" ("status") VALUES {values}')
    _rewind()
    _define_pd3_order(renamed=True, checked=checked)

    report = await _pd3_connect(db_url, updates=True)
    assert schema_steps(report) == []
    if checked:
        assert warning_texts(report) == [_PD3_CHECK_BODY_TEXT, _PD3_STRANDED_TEXT]
    else:
        assert warning_texts(report) == ([_PD3_STRANDED_TEXT] if held else [])

    assert len(_stranded(report)) == (1 if held or checked else 0), report.warnings
    async with ferro.engines.session():
        rows = await fetch_all('SELECT "status" FROM "pd3order" ORDER BY "id"')
    assert [r["status"] for r in rows] == (["canceled", "paid"] if held else ["paid"])


# The row probe a SQLite label-rename hint may cost (ADR-0047; ADR-0011: the
# create pass stays silent about drift). The probe reads a whole unindexed
# column once nothing matches, so it runs only under ``migrate_updates``, and
# only for a column with no ``db_check`` (whose listing already answers).

_PD3_PROBE = ("pd3order", 'SELECT 1 FROM "pd3order" WHERE "status" = \'canceled\' LIMIT 1')


def _probes(report: PassReport) -> list[tuple[str, str]]:
    """The row probes the pass read, as ``(subject, sql)``, in order."""
    return [(s.subject, s.sql) for s in report.statements if s.role == "probe"]


async def _pd3_live_hint(db_url: str, checked: bool, labels: str) -> PassReport:
    """A ``pd3order`` built by the parent model holding ``labels`` rows, and
    the renamed model registered; the report of the pass that built it."""
    _define_pd3_order(renamed=False, checked=checked)
    report = await auto_migrate(db_url)
    async with ferro.engines.session():
        await execute(f'INSERT INTO "pd3order" ("status") VALUES {labels}')
    _rewind()
    _define_pd3_order(renamed=True, checked=checked)
    return report


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_a_plain_auto_migrate_connect_reads_no_rows_for_a_label_hint(
    db_url, clean_registry
):
    """Plain ``auto_migrate`` reconciles nothing, so it neither probes the
    hinted column nor warns, even while a row holds the old label."""
    report = await _pd3_live_hint(db_url, checked=False, labels="('canceled'), ('paid')")
    assert schema_steps(report) == [
        (
            "pd3order",
            'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" varchar(8) NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []

    report = await _pd3_connect(db_url, updates=False)
    assert schema_steps(report) == []
    assert warning_texts(report) == []

    assert _probes(report) == []
    assert _stranded(report) == [], report.warnings


@pytest.mark.asyncio
@pytest.mark.sqlite_only
@pytest.mark.parametrize("built_by", ["parent", "renamed"])
async def test_a_checked_column_decides_a_label_hint_from_its_check_alone(
    db_url, clean_registry, built_by
):
    """A ``db_check`` column's check lists every label a row may hold: a check
    still listing the old label is a live hint (one warning), a check listing
    only the new one an inert hint (silent). Either way no row is read."""
    if built_by == "parent":
        report = await _pd3_live_hint(db_url, checked=True, labels="('canceled'), ('paid')")
        assert schema_steps(report) == [
            (
                "pd3order",
                'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" text NOT NULL CONSTRAINT "ck_pd3order_status" CHECK ("status" IN (\'paid\', \'canceled\')) )',
            ),
        ]
        assert warning_texts(report) == []
    else:
        _define_pd3_order(renamed=True, checked=True)
        report = await auto_migrate(db_url)
        assert schema_steps(report) == [
            (
                "pd3order",
                'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" text NOT NULL CONSTRAINT "ck_pd3order_status" CHECK ("status" IN (\'paid\', \'cancelled\')) )',
            ),
        ]
        assert warning_texts(report) == []
        async with ferro.engines.session():
            await execute('INSERT INTO "pd3order" ("status") VALUES (\'cancelled\')')
        _rewind()
        _define_pd3_order(renamed=True, checked=True)

    report = await _pd3_connect(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == (
        [_PD3_CHECK_BODY_TEXT, _PD3_STRANDED_TEXT] if built_by == "parent" else []
    )

    assert _probes(report) == []
    assert len(_stranded(report)) == (1 if built_by == "parent" else 0), report.warnings


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_an_unchecked_column_probes_its_rows_for_a_label_hint(
    db_url, clean_registry
):
    """With no check to read, ``migrate_updates`` probes the rows: a warning
    while one holds the old label, silence once none does."""
    report = await _pd3_live_hint(db_url, checked=False, labels="('canceled'), ('paid')")
    assert schema_steps(report) == [
        (
            "pd3order",
            'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "status" varchar(8) NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []

    report = await _pd3_connect(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == [
        "Column 'pd3order.status' still holds the enum label 'canceled', which the model now declares as 'cancelled' (__ferro_renamed_labels__). SQLite keeps enum labels as text in the rows, and auto-migrate changes the schema, never the rows: they keep 'canceled', which the model no longer reads or writes. Generate the migration that relabels them with `ferro migrate new`.",
    ]

    assert _probes(report) == [_PD3_PROBE]
    assert len(_stranded(report)) == 1, report.warnings

    async with ferro.engines.session():
        await execute(
            'UPDATE "pd3order" SET "status" = \'cancelled\' WHERE "status" = \'canceled\''
        )
    _rewind()
    _define_pd3_order(renamed=True)

    report = await _pd3_connect(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []

    assert _probes(report) == [_PD3_PROBE]
    assert _stranded(report) == [], report.warnings


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


async def _trn_writer_with_rows(db_url: str) -> PassReport:
    """A populated ``trnwriter``; the report of the pass that built it."""
    _define_trn_writer()
    report = await auto_migrate(db_url)
    async with ferro.engines.session():
        await execute("INSERT INTO \"trnwriter\" (\"name\") VALUES ('Ann'), ('Bo')")
    _rewind()
    return report


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


def _created(report: PassReport) -> list[str]:
    """The tables the pass created, in order."""
    return [
        subject
        for subject, sql in schema_steps(report)
        if sql.startswith("CREATE TABLE")
    ]


def _touched(report: PassReport, prefix: str) -> set[str]:
    """The tables whose name starts with ``prefix`` that the pass changed."""
    return {subject for subject, _ in schema_steps(report) if subject.startswith(prefix)}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_migrate_updates_renames_a_table_and_its_serial_key_with_its_sequence(
    db_url, clean_registry
):
    """``trnwriter`` becomes ``trnauthor`` and its key ``id`` becomes
    ``author_id`` in one pass. Right after the table rename the key is still
    ``id``, so the table's sequence rename names ``id``; the column rename
    then carries the sequence to the name a fresh ``trnauthor`` gives it.
    A second pass has nothing left to do."""
    await _trn_writer_with_rows(db_url)

    class TrnAuthor(Model):
        __ferro_renamed_from__ = "trnwriter"
        author_id: Annotated[
            int | None, FerroField(primary_key=True, renamed_from="id")
        ] = None
        name: Annotated[str, FerroField(index=True)]

    report = await auto_migrate(db_url, updates=True)
    steps = [sql for _, sql in schema_steps(report)]
    assert steps[:4] == [
        'ALTER TABLE "trnwriter" RENAME TO "trnauthor"',
        pg_sequence_rename("trnauthor"),
        'ALTER TABLE "trnauthor" RENAME COLUMN "id" TO "author_id"',
        pg_sequence_rename("trnauthor", "author_id"),
    ]
    assert warning_texts(report) == []
    async with ferro.engines.session():
        rows = await fetch_all(
            "SELECT column_default FROM information_schema.columns WHERE "
            "table_schema = current_schema() AND table_name = 'trnauthor' "
            "AND column_name = 'author_id'"
        )
        assert rows[0]["column_default"] == (
            "nextval('trnauthor_author_id_seq'::regclass)"
        )
        created = await TrnAuthor.create(name="Cy")
        assert created.author_id == 3
    _rewind()

    class TrnAuthor(Model):  # noqa: F811
        __ferro_renamed_from__ = "trnwriter"
        author_id: Annotated[
            int | None, FerroField(primary_key=True, renamed_from="id")
        ] = None
        name: Annotated[str, FerroField(index=True)]

    again = await auto_migrate(db_url, updates=True)
    assert schema_steps(again) == []


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_renames_a_hinted_table_with_its_rows_and_index(
    db_url, db_backend, clean_registry
):
    import warnings

    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _define_trn_author()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == on(
            db_url,
            sqlite=[
                ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
                ("trnauthor", 'DROP INDEX IF EXISTS "idx_trnwriter_name"'),
                (
                    "trnauthor",
                    'CREATE INDEX IF NOT EXISTS "idx_trnauthor_name" ON "trnauthor" ("name")',
                ),
            ],
            postgres=[
                ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
                ("trnauthor", pg_sequence_rename("trnauthor")),
                (
                    "trnauthor",
                    'ALTER INDEX "idx_trnwriter_name" RENAME TO "idx_trnauthor_name"',
                ),
            ],
        )
        assert warning_texts(report) == []
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]

    # The create pass created nothing: the table is the old one, renamed.
    assert "trnauthor" not in _created(report)
    assert "trnauthor" in _touched(report, "trn")
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
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []
    assert "trnauthor" not in _touched(report, "trn")
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]

    # And no drift: the live database read the way `drift` reads it (with
    # the hinted old table) plans to nothing against the models, destructive.
    import json

    from ferro import _core
    from ferro.ir.compiler import compile_registry_schema_ir

    declared = json.dumps(compile_registry_schema_ir())
    live_json, facts_json = await _core._live_schema_ir(None, declared)
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
    assert plan == {"operations": [], "reports": []}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_hinted_table_without_migrate_updates_is_left_alone_and_warns(
    db_url, clean_registry
):
    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _define_trn_author()

    with pytest.warns(
        UserWarning,
        match=(
            r'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter".*'
            r"migrate_updates=True.*ferro migrate new"
        ),
    ):
        report = await auto_migrate(db_url)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter", and the database holds "trnwriter" and no "trnauthor": "trnauthor" was not created. The rename runs under connect(..., migrate_updates=True) or in a migration from ferro migrate new.',
        ]
    assert "trnauthor" not in _created(report)
    assert [w.kind for w in report.warnings] == ["PendingTableRename"]
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
            report = await ferro.create_tables()
            assert schema_steps(report) == []
            assert warning_texts(report) == [
                'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter", and the database holds "trnwriter" and no "trnauthor": "trnauthor" was not created. The rename runs under connect(..., migrate_updates=True) or in a migration from ferro migrate new.',
            ]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_hinted_table_whose_new_name_is_also_live_is_inert(
    db_url, clean_registry
):
    import warnings

    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _define_trn_writer()

    class TrnAuthor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnauthor",
                'CREATE TABLE IF NOT EXISTS "trnauthor" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnauthor",
                'CREATE INDEX IF NOT EXISTS "idx_trnauthor_name" ON "trnauthor" ("name")',
            ),
        ],
        postgres=[
            (
                "trnauthor",
                'CREATE TABLE IF NOT EXISTS "trnauthor" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnauthor",
                'CREATE INDEX IF NOT EXISTS "idx_trnauthor_name" ON "trnauthor" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _rewind()
    _define_trn_author()

    # Both names live: the hint is not live (ADR-0032), so it is inert and
    # silent, and the old table is no business of this modelset.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(db_url, destructive=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == []
    assert not [str(w.message) for w in caught if "trn" in str(w.message)]
    assert _touched(report, "trn") == set()
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter", "trnauthor"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_two_tables_claiming_one_live_old_name_refuse_and_change_nothing(
    db_url, clean_registry
):
    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []

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
    for destructive in (False, True):
        with pytest.warns(UserWarning, match=refusal):
            if destructive:
                report = await auto_migrate(db_url, destructive=True)
                assert schema_steps(report) == []
                assert warning_texts(report) == [
                    'rename hint refused: tables "trnauthor" and "trnpoet" all declare __ferro_renamed_from__ = "trnwriter": one table becomes one table; keep the hint on the model "trnwriter" became',
                ]
            else:
                report = await auto_migrate(db_url)
                assert schema_steps(report) == []
                assert warning_texts(report) == [
                    'rename hint refused: tables "trnauthor" and "trnpoet" all declare __ferro_renamed_from__ = "trnwriter": one table becomes one table; keep the hint on the model "trnwriter" became',
                ]
        # Nothing ran, and the one warning is the refusal.
        assert schema_steps(report) == []
        assert [w.kind for w in report.warnings] == ["HintRefused"]
        assert re.match(refusal, str(report.warnings[0]))
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
    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _define_trn_author_with_books()

    # Without migrate_updates neither is created: "trnbook" can only
    # reference "trnauthor" once the rename has run.
    with pytest.warns(
        UserWarning,
        match=r'"trnauthor" was not created, nor "trnbook", which reference it',
    ):
        report = await auto_migrate(db_url)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter", and the database holds "trnwriter" and no "trnauthor": "trnauthor" was not created, nor "trnbook", which reference it. The rename runs under connect(..., migrate_updates=True) or in a migration from ferro migrate new.',
        ]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter"}

    _rewind()
    _define_trn_author_with_books()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
            ("trnauthor", 'DROP INDEX IF EXISTS "idx_trnwriter_name"'),
            (
                "trnauthor",
                'CREATE INDEX IF NOT EXISTS "idx_trnauthor_name" ON "trnauthor" ("name")',
            ),
            (
                "trnbook",
                'CREATE TABLE IF NOT EXISTS "trnbook" ( "author_id" integer NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL, CONSTRAINT "fk_trnbook_author_id_trnauthor" FOREIGN KEY ("author_id") REFERENCES "trnauthor" ("id") ON DELETE CASCADE )',
            ),
        ],
        postgres=[
            ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
            ("trnauthor", pg_sequence_rename("trnauthor")),
            (
                "trnauthor",
                'ALTER INDEX "idx_trnwriter_name" RENAME TO "idx_trnauthor_name"',
            ),
            (
                "trnbook",
                'CREATE TABLE IF NOT EXISTS "trnbook" ( "author_id" integer NOT NULL, "id" serial PRIMARY KEY NOT NULL, "title" varchar NOT NULL, CONSTRAINT "fk_trnbook_author_id_trnauthor" FOREIGN KEY ("author_id") REFERENCES "trnauthor" ("id") ON DELETE CASCADE )',
            ),
        ],
    )
    assert warning_texts(report) == []
    # The rename runs first, then "trnbook" is created after it, by the
    # reconciliation pass rather than the create pass.
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


def _define_trn_author_with_books_and_appendices() -> None:
    from ferro import ForeignKey

    # "TrnAppendix" sorts ahead of "TrnBook", the table it waits through, so
    # a single pass over the models would see it before it knew to hold it.
    class TrnAppendix(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        heading: str
        book: Annotated["TrnBook", ForeignKey("appendices")]

    class TrnBook(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        author: Annotated["TrnAuthor", ForeignKey("books")]
        appendices: Relation[list["TrnAppendix"]] = BackRef()

    class TrnAuthor(Model):
        __ferro_renamed_from__ = "trnwriter"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]
        books: Relation[list["TrnBook"]] = BackRef()


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_table_two_references_from_a_renamed_one_waits_for_the_rename_too(
    db_url, clean_registry
):
    """``trnappendix`` references ``trnbook``, which references the renamed
    ``trnauthor``: it waits on the rename through ``trnbook``, and is created
    after it, once ``trnbook`` exists."""
    report = await _trn_writer_with_rows(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
        postgres=[
            (
                "trnwriter",
                'CREATE TABLE IF NOT EXISTS "trnwriter" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "trnwriter",
                'CREATE INDEX IF NOT EXISTS "idx_trnwriter_name" ON "trnwriter" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    _define_trn_author_with_books_and_appendices()

    # Without migrate_updates none of the three is created, and the one
    # warning names both dependents.
    with pytest.warns(
        UserWarning,
        match=r'"trnauthor" was not created, nor "trnappendix" and "trnbook", '
        r"which reference it",
    ):
        report = await auto_migrate(db_url)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            'table "trnauthor" declares __ferro_renamed_from__ = "trnwriter", and the database holds "trnwriter" and no "trnauthor": "trnauthor" was not created, nor "trnappendix" and "trnbook", which reference it. The rename runs under connect(..., migrate_updates=True) or in a migration from ferro migrate new.',
        ]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnwriter"}

    _rewind()
    _define_trn_author_with_books_and_appendices()
    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
            ("trnauthor", 'DROP INDEX IF EXISTS "idx_trnwriter_name"'),
            (
                "trnauthor",
                'CREATE INDEX IF NOT EXISTS "idx_trnauthor_name" ON "trnauthor" ("name")',
            ),
            (
                "trnbook",
                'CREATE TABLE IF NOT EXISTS "trnbook" ( "author_id" integer NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL, CONSTRAINT "fk_trnbook_author_id_trnauthor" FOREIGN KEY ("author_id") REFERENCES "trnauthor" ("id") ON DELETE CASCADE )',
            ),
            (
                "trnappendix",
                'CREATE TABLE IF NOT EXISTS "trnappendix" ( "book_id" integer NOT NULL, "heading" varchar NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, CONSTRAINT "fk_trnappendix_book_id_trnbook" FOREIGN KEY ("book_id") REFERENCES "trnbook" ("id") ON DELETE CASCADE )',
            ),
        ],
        postgres=[
            ("trnauthor", 'ALTER TABLE "trnwriter" RENAME TO "trnauthor"'),
            ("trnauthor", pg_sequence_rename("trnauthor")),
            (
                "trnauthor",
                'ALTER INDEX "idx_trnwriter_name" RENAME TO "idx_trnauthor_name"',
            ),
            (
                "trnbook",
                'CREATE TABLE IF NOT EXISTS "trnbook" ( "author_id" integer NOT NULL, "id" serial PRIMARY KEY NOT NULL, "title" varchar NOT NULL, CONSTRAINT "fk_trnbook_author_id_trnauthor" FOREIGN KEY ("author_id") REFERENCES "trnauthor" ("id") ON DELETE CASCADE )',
            ),
            (
                "trnappendix",
                'CREATE TABLE IF NOT EXISTS "trnappendix" ( "book_id" integer NOT NULL, "heading" varchar NOT NULL, "id" serial PRIMARY KEY NOT NULL, CONSTRAINT "fk_trnappendix_book_id_trnbook" FOREIGN KEY ("book_id") REFERENCES "trnbook" ("id") ON DELETE CASCADE )',
            ),
        ],
    )
    assert warning_texts(report) == []
    # Each table is created after the one it references: the appendix's
    # foreign key would fail against a book that did not exist yet.
    async with ferro.engines.session():
        await execute(
            'INSERT INTO "trnbook" ("title", "author_id") VALUES (\'Odes\', 2)'
        )
        await execute(
            'INSERT INTO "trnappendix" ("heading", "book_id") VALUES (\'I\', 1)'
        )
        rows = await fetch_all(
            'SELECT "trnauthor"."name" FROM "trnappendix" '
            'JOIN "trnbook" ON "trnbook"."id" = "trnappendix"."book_id" '
            'JOIN "trnauthor" ON "trnauthor"."id" = "trnbook"."author_id"'
        )
        assert [r["name"] for r in rows] == ["Bo"]
    ferro.reset_engine()
    assert await _trn_tables(db_url) == {"trnauthor", "trnbook", "trnappendix"}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_live_label_rename_on_postgres_is_only_the_rename(db_url, clean_registry):
    """``__ferro_renamed_labels__ = {"cancelled": "canceled"}`` on a live
    Postgres type is one ``ALTER TYPE … RENAME VALUE`` and nothing else: the
    renamed type already holds ``cancelled``, so no ``ADD VALUE 'cancelled'``
    follows, and ``canceled`` is gone, so no warning calls it a label the
    model no longer declares."""
    import warnings

    _define_pd3_order(renamed=False)
    report = await auto_migrate(db_url)
    assert schema_steps(report) == [
        (
            "pd3status",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typname = 'pd3status' AND n.nspname = current_schema()) THEN CREATE TYPE \"pd3status\" AS ENUM ('paid', 'canceled'); END IF; END $$",
        ),
        (
            "pd3order",
            'CREATE TABLE IF NOT EXISTS "pd3order" ( "id" serial PRIMARY KEY NOT NULL, "status" pd3status NOT NULL )',
        ),
    ]
    assert warning_texts(report) == []
    async with ferro.engines.session():
        await execute("INSERT INTO \"pd3order\" (\"status\") VALUES ('canceled')")
    _rewind()
    _define_pd3_order(renamed=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(db_url, updates=True)
        assert schema_steps(report) == [
            ("pd3status", "ALTER TYPE \"pd3status\" RENAME VALUE 'canceled' TO 'cancelled'"),
        ]
        assert warning_texts(report) == []

    executed = [sql for _, sql in schema_steps(report)]
    assert [sql for sql in executed if "ALTER TYPE" in sql] == [
        "ALTER TYPE \"pd3status\" RENAME VALUE 'canceled' TO 'cancelled'"
    ], executed
    assert not [
        str(w.message) for w in caught if "no longer declares" in str(w.message)
    ], [str(w.message) for w in caught]
    async with ferro.engines.session():
        rows = await fetch_all('SELECT "status" FROM "pd3order"')
    assert [r["status"] for r in rows] == ["cancelled"]


# ---------------------------------------------------------------------------
# A name held by something that is not a table
# ---------------------------------------------------------------------------
#
# A view named like a model's table:
#
#     CREATE VIEW "heldcard" AS SELECT 1 AS "id", 'x' AS "label";
#
# `CREATE TABLE IF NOT EXISTS "heldcard"` would skip in silence, and the
# connect would return as if `HeldCard` had its table. The create pass refuses
# instead, before any DDL:
#
#     MigrationRefused: Table creation is refused: a declared table's name is
#     held by something that is not a table, ...
#       "heldcard" is a view: rename or drop the view, or declare a different
#       __ferro_table__ on app.models.HeldCard.
#     Nothing was created.
#
# The model is named by its full identity (module and qualified name), so the
# line points at the one class to edit.

HELD_REFUSAL = (
    "Table creation is refused: a declared table's name is held by something "
    "that is not a table, so CREATE TABLE would skip it and leave the model "
    "without one.\n"
    '  "heldcard" is a view: rename or drop the view, or declare a different '
    f"__ferro_table__ on {__name__}._define_held_models.<locals>.HeldCard.\n"
    "Nothing was created."
)

HELD_VIEW = 'CREATE VIEW "heldcard" AS SELECT 1 AS "id", \'x\' AS "label"'


def _define_held_models() -> None:
    class HeldCard(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    class HeldDeck(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: Annotated[str, FerroField(index=True)]


async def _held_relations(db_url: str, db_backend: str) -> dict[str, str]:
    """Every ``held*`` table or view in the schema, with its catalog kind."""
    await ferro.connect(db_url)
    async with ferro.engines.session():
        if db_backend == "sqlite":
            rows = await fetch_all(
                "SELECT name, type AS kind FROM sqlite_master "
                "WHERE name LIKE 'held%' AND type IN ('table', 'view')"
            )
        else:
            rows = await fetch_all(
                "SELECT table_name::text AS name, table_type::text AS kind "
                "FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'held%'"
            )
    ferro.reset_engine()
    return {row["name"]: row["kind"].lower() for row in rows}


async def _seed_held(db_url: str, sql: str) -> None:
    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(sql)
    ferro.reset_engine()


@pytest.mark.asyncio
@pytest.mark.backend_matrix
@pytest.mark.parametrize(
    "flags",
    [{"auto_migrate": True}, {"migrate_updates": True}],
    ids=lambda flags: next(iter(flags)),
)
async def test_a_view_named_like_a_model_refuses_connect_and_creates_nothing(
    db_url, db_backend, clean_registry, flags
):
    from ferro.migrations import MigrationRefused

    await _seed_held(db_url, HELD_VIEW)
    _define_held_models()

    with pytest.raises(MigrationRefused) as raised:
        await ferro.connect(db_url, **flags)

    assert str(raised.value) == HELD_REFUSAL
    # Refused before any DDL: the error's report is empty.
    assert raised.value.report == PassReport()
    # Nothing was created: not the model the view stands in for, not the
    # model beside it.
    ferro.reset_engine()
    assert await _held_relations(db_url, db_backend) == {"heldcard": "view"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_create_tables_refuses_a_view_named_like_a_model(
    db_url, db_backend, clean_registry
):
    from ferro.migrations import MigrationRefused

    await _seed_held(db_url, HELD_VIEW)
    _define_held_models()

    await ferro.connect(db_url)
    with pytest.raises(MigrationRefused) as raised:
        await ferro.create_tables()

    assert schema_steps(raised.value.report) == []
    assert warning_texts(raised.value.report) == []
    assert str(raised.value) == HELD_REFUSAL
    ferro.reset_engine()
    assert await _held_relations(db_url, db_backend) == {"heldcard": "view"}


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_base_table_named_like_a_model_is_left_to_reconcile_as_before(
    db_url, db_backend, clean_registry
):
    """The refusal is about what holds the name, never about its being
    live: a base table of the model's name is the reconciliation pass's, and
    the model beside it is created."""
    await _seed_held(
        db_url, 'CREATE TABLE "heldcard" ("id" integer PRIMARY KEY, "label" text)'
    )
    _define_held_models()

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "helddeck",
                'CREATE TABLE IF NOT EXISTS "helddeck" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL )',
            ),
            (
                "helddeck",
                'CREATE INDEX IF NOT EXISTS "idx_helddeck_name" ON "helddeck" ("name")',
            ),
        ],
        postgres=[
            (
                "helddeck",
                'CREATE TABLE IF NOT EXISTS "helddeck" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL )',
            ),
            (
                "helddeck",
                'CREATE INDEX IF NOT EXISTS "idx_helddeck_name" ON "helddeck" ("name")',
            ),
        ],
    )
    assert warning_texts(report) == []
    ferro.reset_engine()

    table = "table" if db_backend == "sqlite" else "base table"
    assert await _held_relations(db_url, db_backend) == {
        "heldcard": table,
        "helddeck": table,
    }


# -- a redefined index and a removed foreign key (ADR-0051) ------------------------


def _by_hand(db_url: str, db_backend: str, sql: str, *, fetch: bool = False):
    """Run ``sql`` on a plain driver connection, outside ferro."""
    if db_backend == "sqlite":
        import sqlite3

        conn = sqlite3.connect(
            db_url.removeprefix("sqlite:").split("?", 1)[0], isolation_level=None
        )
    else:
        import psycopg

        m = re.search(r"search_path=([^&]+)", db_url)
        schema = m.group(1) if m else "public"
        base_url = db_url.replace("postgres://", "postgresql://", 1).split("?")[0]
        conn = psycopg.connect(
            base_url, options=f"-c search_path={schema}", autocommit=True
        )
    try:
        cursor = conn.execute(sql)
        return cursor.fetchall() if fetch else None
    finally:
        conn.close()


def _index_definition(
    db_url: str, db_backend: str, index: str
) -> tuple[list[str], bool]:
    """The live columns and uniqueness of ``index``."""
    if db_backend == "sqlite":
        columns = _by_hand(
            db_url,
            db_backend,
            f"SELECT name FROM pragma_index_info('{index}') ORDER BY seqno",
            fetch=True,
        )
        ((sql,),) = _by_hand(
            db_url,
            db_backend,
            f"SELECT sql FROM sqlite_master WHERE type = 'index' AND name = '{index}'",
            fetch=True,
        )
        return [name for (name,) in columns], sql.startswith("CREATE UNIQUE INDEX")
    rows = _by_hand(
        db_url,
        db_backend,
        "SELECT a.attname::text, i.indisunique FROM pg_index i JOIN pg_attribute a "
        "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        f"WHERE i.indexrelid = '\"{index}\"'::regclass "
        "ORDER BY array_position(i.indkey::smallint[], a.attnum)",
        fetch=True,
    )
    return [name for name, _ in rows], bool(rows[0][1])


TRUNCATED = "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx"


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_redefines_an_index_whose_cut_name_now_covers_more_columns(
    db_url, db_backend, clean_registry
):
    """Both column groups cut to one 63-character name: the pass used to
    leave the old index under it. Redefined without migrate_destructive."""
    from typing import ClassVar

    class SubscriptionInvoiceLine(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("billing_period_start", "billing_period_end"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        billing_period_start: int
        billing_period_end: int
        customer_id: int

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "subscriptioninvoiceline",
                'CREATE TABLE IF NOT EXISTS "subscriptioninvoiceline" ( "billing_period_end" integer NOT NULL, "billing_period_start" integer NOT NULL, "customer_id" integer NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )',
            ),
            (
                "subscriptioninvoiceline",
                'CREATE INDEX IF NOT EXISTS "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx" ON "subscriptioninvoiceline" ("billing_period_start", "billing_period_end")',
            ),
        ],
        postgres=[
            (
                "subscriptioninvoiceline",
                'CREATE TABLE IF NOT EXISTS "subscriptioninvoiceline" ( "billing_period_end" integer NOT NULL, "billing_period_start" integer NOT NULL, "customer_id" integer NOT NULL, "id" serial PRIMARY KEY NOT NULL )',
            ),
            (
                "subscriptioninvoiceline",
                'CREATE INDEX IF NOT EXISTS "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx" ON "subscriptioninvoiceline" ("billing_period_start", "billing_period_end")',
            ),
        ],
    )
    assert warning_texts(report) == []
    assert _index_definition(db_url, db_backend, TRUNCATED) == (
        ["billing_period_start", "billing_period_end"],
        False,
    )
    _rewind()

    class SubscriptionInvoiceLine(Model):  # noqa: F811 — intentional redefinition
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("billing_period_start", "billing_period_end", "customer_id"),
        )
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        billing_period_start: int
        billing_period_end: int
        customer_id: int

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        (
            "subscriptioninvoiceline",
            'DROP INDEX IF EXISTS "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx"',
        ),
        (
            "subscriptioninvoiceline",
            'CREATE INDEX IF NOT EXISTS "idx_subscriptioninvoiceline_billing_period_start_billing_pe_idx" ON "subscriptioninvoiceline" ("billing_period_start", "billing_period_end", "customer_id")',
        ),
    ]
    assert warning_texts(report) == []
    assert _index_definition(db_url, db_backend, TRUNCATED) == (
        ["billing_period_start", "billing_period_end", "customer_id"],
        False,
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_migrate_updates_redefines_a_ferro_named_index_written_another_way(
    db_url, db_backend, clean_registry
):
    """A live ``idx_`` index over other columns (a hand edit, an older
    build) is replaced by the declared one."""

    class RedefLive(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        code: Annotated[str, FerroField(index=True)]
        label: str

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "redeflive",
                'CREATE TABLE IF NOT EXISTS "redeflive" ( "code" varchar NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "label" varchar NOT NULL )',
            ),
            (
                "redeflive",
                'CREATE INDEX IF NOT EXISTS "idx_redeflive_code" ON "redeflive" ("code")',
            ),
        ],
        postgres=[
            (
                "redeflive",
                'CREATE TABLE IF NOT EXISTS "redeflive" ( "code" varchar NOT NULL, "id" serial PRIMARY KEY NOT NULL, "label" varchar NOT NULL )',
            ),
            (
                "redeflive",
                'CREATE INDEX IF NOT EXISTS "idx_redeflive_code" ON "redeflive" ("code")',
            ),
        ],
    )
    assert warning_texts(report) == []
    ferro.reset_engine()
    _by_hand(db_url, db_backend, 'DROP INDEX "idx_redeflive_code"')
    _by_hand(
        db_url, db_backend, 'CREATE INDEX "idx_redeflive_code" ON "redeflive" ("label")'
    )

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == [
        ("redeflive", 'DROP INDEX IF EXISTS "idx_redeflive_code"'),
        (
            "redeflive",
            'CREATE INDEX IF NOT EXISTS "idx_redeflive_code" ON "redeflive" ("code")',
        ),
    ]
    assert warning_texts(report) == []
    assert _index_definition(db_url, db_backend, "idx_redeflive_code") == (
        ["code"],
        False,
    )


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_unique_redefinition_over_duplicates_fails_counted_naming_the_fix(
    db_url, db_backend, clean_registry
):
    """A live non-unique ``uq_`` index the model declares unique: the
    redefinition builds a unique index, which duplicates refuse. The pass
    fails with the count and the fix."""

    class RedefDupe(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        code: str

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "redefdupe",
                'CREATE TABLE IF NOT EXISTS "redefdupe" ( "code" varchar NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )',
            ),
        ],
        postgres=[
            (
                "redefdupe",
                'CREATE TABLE IF NOT EXISTS "redefdupe" ( "code" varchar NOT NULL, "id" serial PRIMARY KEY NOT NULL )',
            ),
        ],
    )
    assert warning_texts(report) == []
    async with ferro.engines.session():
        for code in ("a", "a", "b", "b", "c"):
            await RedefDupe.create(code=code)
    _rewind()
    _by_hand(
        db_url, db_backend, 'CREATE INDEX "uq_redefdupe_code" ON "redefdupe" ("code")'
    )

    class RedefDupe(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        code: Annotated[str, FerroField(unique=True)]

    with pytest.raises(Exception) as failure:
        await auto_migrate(db_url, updates=True)
    # The failing CREATE UNIQUE INDEX is the error's to name. What committed
    # before it is the report's: on SQLite (statement at a time) the DROP of
    # the old index stood; on Postgres the table's unit rolled back whole.
    assert schema_steps(failure.value.report) == on(
        db_url,
        sqlite=[
            ("redefdupe", 'DROP INDEX IF EXISTS "uq_redefdupe_code"'),
        ],
        postgres=[],
    )
    assert warning_texts(failure.value.report) == []
    message = str(failure.value)
    assert '2 values are duplicated under "uq_redefdupe_code" on "redefdupe"' in message
    assert "fix the rows" in message
    if db_backend == "postgres":
        # One transaction per table: the old index is still there.
        assert _index_definition(db_url, db_backend, "uq_redefdupe_code") == (
            ["code"],
            False,
        )


def _live_foreign_keys(db_url: str, db_backend: str, table: str) -> list[str]:
    """The columns of ``table`` a live foreign key constrains."""
    if db_backend == "sqlite":
        rows = _by_hand(
            db_url, db_backend, f"PRAGMA foreign_key_list(\"{table}\")", fetch=True
        )
        return sorted(row[3] for row in rows)
    rows = _by_hand(
        db_url,
        db_backend,
        "SELECT a.attname::text FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        f"WHERE c.contype = 'f' AND c.conrelid = '\"{table}\"'::regclass",
        fetch=True,
    )
    return sorted(name for (name,) in rows)


@pytest.mark.asyncio
@pytest.mark.backend_matrix
async def test_a_foreign_key_removed_from_a_kept_column_is_dropped_only_when_destructive(
    db_url, db_backend, clean_registry
):
    """``team: Annotated[DfkTeam, ForeignKey(...)]`` becomes ``team_id: int``:
    the column stays and its constraint is ferro's. ``migrate_updates``
    leaves it (ADR-0013's ladder); ``migrate_destructive`` drops it on
    Postgres. SQLite cannot drop a table constraint in place: it warns,
    naming the reviewed path, and the constraint stays."""
    from ferro import ForeignKey

    class DfkTeam(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        members: Relation[list["DfkMember"]] = BackRef()

    class DfkMember(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team: Annotated[DfkTeam | None, ForeignKey(related_name="members")] = None

    report = await auto_migrate(db_url)
    assert schema_steps(report) == on(
        db_url,
        sqlite=[
            (
                "dfkteam",
                'CREATE TABLE IF NOT EXISTS "dfkteam" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT )',
            ),
            (
                "dfkmember",
                'CREATE TABLE IF NOT EXISTS "dfkmember" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "team_id" integer, CONSTRAINT "fk_dfkmember_team_id_dfkteam" FOREIGN KEY ("team_id") REFERENCES "dfkteam" ("id") ON DELETE CASCADE )',
            ),
        ],
        postgres=[
            (
                "dfkteam",
                'CREATE TABLE IF NOT EXISTS "dfkteam" ( "id" serial PRIMARY KEY NOT NULL )',
            ),
            (
                "dfkmember",
                'CREATE TABLE IF NOT EXISTS "dfkmember" ( "id" serial PRIMARY KEY NOT NULL, "team_id" integer, CONSTRAINT "fk_dfkmember_team_id_dfkteam" FOREIGN KEY ("team_id") REFERENCES "dfkteam" ("id") ON DELETE CASCADE )',
            ),
        ],
    )
    assert warning_texts(report) == []
    assert _live_foreign_keys(db_url, db_backend, "dfkmember") == ["team_id"]
    _rewind()

    class DfkTeam(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    class DfkMember(Model):  # noqa: F811 — intentional redefinition
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        team_id: int | None = None

    report = await auto_migrate(db_url, updates=True)
    assert schema_steps(report) == []
    assert warning_texts(report) == []
    assert _live_foreign_keys(db_url, db_backend, "dfkmember") == ["team_id"]
    ferro.reset_engine()

    if db_backend == "postgres":
        report = await auto_migrate(db_url, destructive=True)
        assert schema_steps(report) == [
            (
                "dfkmember",
                'ALTER TABLE "dfkmember" DROP CONSTRAINT "fk_dfkmember_team_id_dfkteam"',
            ),
        ]
        assert warning_texts(report) == []
        assert _live_foreign_keys(db_url, db_backend, "dfkmember") == []
        return
    with pytest.warns(UserWarning, match="fk_dfkmember_team_id_dfkteam"):
        report = await auto_migrate(db_url, destructive=True)
        assert schema_steps(report) == []
        assert warning_texts(report) == [
            "Foreign key 'fk_dfkmember_team_id_dfkteam' on 'dfkmember.team_id' is no longer declared, and SQLite cannot drop a table constraint in place, so it stays and keeps enforcing its reference. Generate a reviewed migration with `ferro migrate new` to rebuild the table without it.",
        ]
    assert _live_foreign_keys(db_url, db_backend, "dfkmember") == ["team_id"]


# -- a move to or from a native enum type: the pass reports the generator's recipe --


def _enum_move_models(native: bool, kept: bool = False) -> dict:
    """``EnumMove`` with ``mood`` declared as a ``StrEnum`` (a native type on
    Postgres), as one kept in a ``text`` column (``kept``), or as ``str``;
    returns the declared modelset."""
    from enum import StrEnum

    from ferro import clear_registry, ensure_resolved_modelset, reset_engine
    from ferro.registry import REGISTRY

    reset_engine()
    clear_registry()
    REGISTRY.reset_for_test()

    class MoveMood(StrEnum):
        CALM = "calm"
        LOUD = "loud"

    if kept:

        class EnumMove(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            mood: Annotated[MoveMood | None, FerroField(db_type="text")] = None

    elif native:

        class EnumMove(Model):  # noqa: F811 - the same model, edited
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            mood: MoveMood | None = None

    else:

        class EnumMove(Model):  # noqa: F811 - the same model, edited
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            mood: str | None = None

    return ensure_resolved_modelset()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_before", [True, False], ids=["from-enum", "to-enum"])
async def test_a_move_to_or_from_a_native_enum_type_reports_the_generators_recipe(
    db_url, db_backend, clean_registry, native_before
):
    """``mood: MoveMood`` becomes ``mood: str`` (or back). No statement
    converts a column to or from a native Postgres enum type in place, so the
    pass runs no DDL for it and says so in the generator's own words, the
    recipe ``ferro migrate new`` refuses with (ADR-0052, ``EnumTypeMove``).
    A move *to* the enum type also names the fix that keeps the values in a
    text column, ``db_type="text"``, and that fix ends the report. SQLite
    stores an enum as text: there is no type to move, and nothing to say."""
    import json

    from ferro import _core

    before = _enum_move_models(native_before)
    await auto_migrate(db_url)
    after = _enum_move_models(not native_before)
    report = await warned_auto_migrate(db_url, updates=True)

    assert schema_steps(report) == []
    if db_backend == "sqlite":
        assert warning_texts(report) == []
        return
    recipe = (
        '"enummove"."mood" moves to or from a native enum type, which no statement '
        "converts in place: add a column of the new type, copy the values across in a "
        "data step (ferro migrate new --data-step …), then drop the old column"
    )
    if not native_before:
        recipe += (
            "; or keep the values in a text column by declaring the field with "
            'db_type="text"'
        )
    assert [(w.kind, w.text) for w in report.warnings] == [("EnumTypeMove", recipe)]
    with pytest.raises(Exception) as refused:
        _core._generate_migration(json.dumps(before), json.dumps(after), ["postgres"])
    assert str(refused.value) == recipe

    if not native_before:
        # The fix the text names keeps the values (varchar widens to text)
        # and ends the report.
        _enum_move_models(True, kept=True)
        kept = await auto_migrate(db_url, updates=True)
        assert warning_texts(kept) == []
        assert schema_steps(kept) == [
            (
                "enummove",
                'ALTER TABLE "enummove" ALTER COLUMN "mood" TYPE text USING "mood"::text',
            )
        ]
        ferro.reset_engine()
        again = await auto_migrate(db_url, updates=True)
        assert (schema_steps(again), warning_texts(again)) == ([], [])
