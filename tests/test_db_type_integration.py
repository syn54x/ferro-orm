"""Integration tests for db_type / db_check: live DDL and round-trips.

Unit tests in test_alembic_db_type.py and test_db_type_cross_emitter_parity.py
compare rendered SQL strings. These tests run connect(auto_migrate=True) against
real SQLite and Postgres and verify schema introspection plus ORM round-trips.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from uuid import UUID, uuid4

import pytest

from ferro import Field, Model, clear_registry, connect, reset_engine, varchar

pytestmark = pytest.mark.backend_matrix


class _FileFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"


@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    yield
    reset_engine()
    clear_registry()


def _sqlite_column_type(db_url: str, table: str, column: str) -> str:
    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info('{table.lower()}')")
    columns = {row[1]: row[2].upper() for row in cursor.fetchall()}
    conn.close()
    if column not in columns:
        raise AssertionError(
            f"Column {column!r} not found on {table!r}; have {list(columns)}"
        )
    return columns[column]


def _postgres_column(
    postgres_base_url: str, schema_name: str, table: str, column: str
) -> tuple[str, str]:
    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{schema_name}"')
        row = conn.execute(
            """
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
              AND column_name = %s
            """,
            (schema_name, table.lower(), column),
        ).fetchone()
    assert row is not None, (
        f"Column {table}.{column} not found in schema {schema_name!r}"
    )
    return row[0], row[1]


def _postgres_check_constraints(
    postgres_base_url: str, schema_name: str, table: str
) -> list[tuple[str, str]]:
    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{schema_name}"')
        rows = conn.execute(
            """
            SELECT con.conname, pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            WHERE nsp.nspname = %s
              AND rel.relname = %s
              AND con.contype = 'c'
            """,
            (schema_name, table.lower()),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _postgres_enum_types(
    postgres_base_url: str, schema_name: str
) -> list[str]:
    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{schema_name}"')
        rows = conn.execute(
            """
            SELECT t.typname
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = %s
              AND t.typtype = 'e'
            """,
            (schema_name,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Runtime DDL (auto_migrate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_strenum_db_type_text_runtime_ddl_sqlite(db_url):
    """auto_migrate stores StrEnum as TEXT, not a separate enum artifact."""

    class TextFormatDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text")

    await connect(db_url, auto_migrate=True)

    col_type = _sqlite_column_type(db_url, "textformatdoc", "format")
    assert "TEXT" in col_type, f"expected TEXT storage, got {col_type!r}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_strenum_db_type_text_runtime_ddl_postgres(
    db_url, postgres_base_url, db_schema_name
):
    class TextFormatDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text")

    await connect(db_url, auto_migrate=True)

    data_type, udt_name = _postgres_column(
        postgres_base_url, db_schema_name, "textformatdoc", "format"
    )
    assert data_type == "text" or udt_name == "text", (
        f"expected text column, got data_type={data_type!r} udt_name={udt_name!r}"
    )
    enum_types = _postgres_enum_types(postgres_base_url, db_schema_name)
    assert "fileformat" not in enum_types and "textformatdoc" not in enum_types, (
        f"db_type=text should not create a native enum type; found {enum_types!r}"
    )


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_db_check_constraint_exists_after_auto_migrate(
    db_url, postgres_base_url, db_schema_name
):
    class CheckedDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text", db_check=True)

    await connect(db_url, auto_migrate=True)

    checks = _postgres_check_constraints(
        postgres_base_url, db_schema_name, "checkeddoc"
    )
    matching = [c for c in checks if c[0] == "ck_checkeddoc_format"]
    assert len(matching) == 1, f"expected ck_checkeddoc_format, got {checks!r}"
    definition = matching[0][1].lower()
    assert "format" in definition and "pdf" in definition and "json" in definition


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_db_check_not_emitted_on_sqlite_runtime_ddl(db_url):
    """Rust emitter elides post-create CHECK on SQLite (Phase 1)."""

    class FormatRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text", db_check=True)

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND tbl_name = 'formatrow'"
    )
    all_sql = " ".join((row[0] or "") for row in cursor.fetchall()).upper()
    conn.close()
    assert " CHECK (" not in all_sql and " CHECK(" not in all_sql, (
        "SQLite runtime DDL should not emit CHECK constraints for db_check in Phase 1; "
        f"got: {all_sql!r}"
    )


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_bigint_db_type_runtime_ddl_sqlite(db_url):
    class BigintCounter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int = Field(db_type="bigint")

    await connect(db_url, auto_migrate=True)

    col_type = _sqlite_column_type(db_url, "bigintcounter", "value")
    assert "BIGINT" in col_type, f"expected BIGINT keyword, got {col_type!r}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_bigint_db_type_runtime_ddl_postgres(
    db_url, postgres_base_url, db_schema_name
):
    class BigintCounter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int = Field(db_type="bigint")

    await connect(db_url, auto_migrate=True)

    data_type, udt_name = _postgres_column(
        postgres_base_url, db_schema_name, "bigintcounter", "value"
    )
    assert data_type == "bigint" or udt_name == "int8", (
        f"expected bigint, got data_type={data_type!r} udt_name={udt_name!r}"
    )


# ---------------------------------------------------------------------------
# ORM round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strenum_text_storage_round_trip(db_url):
    class TextFormatDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text")

    await connect(db_url, auto_migrate=True)

    created = await TextFormatDoc.create(format=_FileFormat.JSON)
    fetched = await TextFormatDoc.get(created.id)
    assert fetched is not None
    assert fetched.format == _FileFormat.JSON

    updated = await TextFormatDoc.where(TextFormatDoc.id == created.id).update(
        format=_FileFormat.PDF
    )
    assert updated == 1
    again = await TextFormatDoc.get(created.id)
    assert again is not None
    assert again.format == _FileFormat.PDF


@pytest.mark.asyncio
async def test_bigint_value_round_trip(db_url):
    class BigintCounter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int = Field(db_type="bigint")

    await connect(db_url, auto_migrate=True)

    large = 9_000_000_000
    row = await BigintCounter.create(value=large)
    fetched = await BigintCounter.get(row.id)
    assert fetched is not None
    assert fetched.value == large


@pytest.mark.asyncio
async def test_uuid_stored_as_text_round_trip(db_url):
    class UuidTextRecord(Model):
        id: int | None = Field(default=None, primary_key=True)
        external_id: UUID = Field(db_type="text")

    await connect(db_url, auto_migrate=True)

    uid = uuid4()
    row = await UuidTextRecord.create(external_id=uid)
    fetched = await UuidTextRecord.get(row.id)
    assert fetched is not None
    assert fetched.external_id == uid


@pytest.mark.asyncio
async def test_varchar_helper_round_trip(db_url):
    class CodeRow(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str = Field(db_type=varchar(32))

    await connect(db_url, auto_migrate=True)

    row = await CodeRow.create(code="ABC-123")
    fetched = await CodeRow.get(row.id)
    assert fetched is not None
    assert fetched.code == "ABC-123"


# ---------------------------------------------------------------------------
# db_check enforcement (Postgres only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_db_check_rejects_invalid_value_on_insert(
    db_url, postgres_base_url, db_schema_name
):
    class CheckedDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat = Field(db_type="text", db_check=True)

    await connect(db_url, auto_migrate=True)
    await CheckedDoc.create(format=_FileFormat.PDF)

    import psycopg
    from psycopg import errors

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO checkeddoc (format) VALUES ('not-a-valid-format')"
            )
            conn.commit()


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_default_strenum_without_db_type_runtime_ddl_and_round_trip(
    db_url, postgres_base_url, db_schema_name
):
    """Regression: StrEnum without db_type still works end-to-end via auto_migrate.

    The Rust runtime emitter stores enum values in a string column (varchar/text),
    not as a native PostgreSQL ENUM type. Native enum DDL is an Alembic-bridge
    concern — see ``test_alembic_db_type.py::test_default_enum_still_renders_named_sa_enum``.
    """

    class NativeEnumDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _FileFormat

    await connect(db_url, auto_migrate=True)

    data_type, udt_name = _postgres_column(
        postgres_base_url, db_schema_name, "nativeenumdoc", "format"
    )
    assert udt_name in ("varchar", "text") or data_type in (
        "character varying",
        "text",
    ), (
        f"Rust auto_migrate should use string storage for default StrEnum, "
        f"got data_type={data_type!r} udt_name={udt_name!r}"
    )
    enum_types = _postgres_enum_types(postgres_base_url, db_schema_name)
    assert "fileformat" not in enum_types, (
        "auto_migrate should not create a native PG enum type without db_type"
    )

    row = await NativeEnumDoc.create(format=_FileFormat.JSON)
    fetched = await NativeEnumDoc.get(row.id)
    assert fetched is not None
    assert fetched.format == _FileFormat.JSON
