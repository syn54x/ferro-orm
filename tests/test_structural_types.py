import pytest
import uuid
from decimal import Decimal
from enum import Enum
from typing import Annotated, Dict, List
from ferro import Model, connect, FerroField

pytestmark = pytest.mark.backend_matrix


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


class TranscriptFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    TXT = "txt"
    JSON = "json"


@pytest.mark.asyncio
async def test_structural_types_roundtrip(db_url):
    """Test that UUID, JSON, Enum, BLOB, and Decimal objects are correctly saved and hydrated."""

    class ComplexModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        user_id: uuid.UUID
        metadata: Dict[str, str]
        tags: List[str]
        role: UserRole
        data: bytes
        balance: Decimal

    await connect(db_url, auto_migrate=True)

    uid = uuid.uuid4()
    raw_data = b"hello world"
    balance = Decimal("123.456")

    item = await ComplexModel.create(
        user_id=uid,
        metadata={"key": "value"},
        tags=["a", "b"],
        role=UserRole.ADMIN,
        data=raw_data,
        balance=balance,
    )
    item_id = item.id

    # Force eviction from Identity Map to test database hydration
    from ferro import evict_instance

    evict_instance("ComplexModel", str(item_id))

    fetched = await ComplexModel.get(item_id)
    assert fetched is not None

    # Assertions
    assert isinstance(fetched.user_id, uuid.UUID)
    assert fetched.user_id == uid

    assert isinstance(fetched.metadata, dict)
    assert fetched.metadata == {"key": "value"}

    assert isinstance(fetched.tags, list)
    assert fetched.tags == ["a", "b"]

    assert isinstance(fetched.role, UserRole)
    assert fetched.role == UserRole.ADMIN

    assert isinstance(fetched.data, bytes)
    assert fetched.data == raw_data

    assert isinstance(fetched.balance, Decimal)
    assert fetched.balance == balance


@pytest.mark.asyncio
async def test_structural_filtering(db_url):
    """Test filtering by UUID and Decimal."""

    class ComplexModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        user_id: uuid.UUID
        balance: Decimal

    await connect(db_url, auto_migrate=True)

    uid1 = uuid.uuid4()
    uid2 = uuid.uuid4()

    await ComplexModel.create(user_id=uid1, balance=Decimal("10.0"))
    await ComplexModel.create(user_id=uid2, balance=Decimal("20.0"))

    # Filter by UUID
    res = await ComplexModel.where(ComplexModel.user_id == uid1).first()
    assert res is not None
    assert res.user_id == uid1

    # Filter by Decimal
    res = await ComplexModel.where(ComplexModel.balance > Decimal("15.0")).first()
    assert res is not None
    assert res.balance == Decimal("20.0")


@pytest.mark.asyncio
async def test_uuid_in_filter_serializes_collection_values(db_url):
    """UUID values inside IN filters should serialize the same way as scalar UUID filters."""

    class ComplexModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        user_id: uuid.UUID

    await connect(db_url, auto_migrate=True)

    uid1 = uuid.uuid4()
    uid2 = uuid.uuid4()
    uid3 = uuid.uuid4()

    await ComplexModel.create(user_id=uid1)
    await ComplexModel.create(user_id=uid2)
    await ComplexModel.create(user_id=uid3)

    results = await ComplexModel.where(ComplexModel.user_id << [uid1, uid3]).all()
    assert {row.user_id for row in results} == {uid1, uid3}


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_postgres_enum_column_decodes_via_text_cast(
    db_url, postgres_base_url, db_schema_name
):
    """Reading a native Postgres enum column should not fail through sqlx::Any."""

    class Transcript(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        format: TranscriptFormat

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute(
            "CREATE TYPE transcriptformat AS ENUM ('pdf', 'docx', 'md', 'txt', 'json')"
        )
        conn.execute(
            """
            CREATE TABLE transcript (
                id integer PRIMARY KEY,
                format transcriptformat NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO transcript (id, format) VALUES (1, 'pdf')")
        conn.commit()

    await connect(db_url)

    fetched = await Transcript.get(1)
    assert fetched is not None
    assert fetched.format == TranscriptFormat.PDF


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_postgres_enum_plain_str_column(
    db_url, postgres_base_url, db_schema_name
):
    """Native PG enum columns work when the model field is plain `str` (no `enum_type_name` in schema)."""

    class StrFieldEnumModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: str

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute(
            """
            CREATE TYPE strfieldrowstatus AS ENUM ('active', 'inactive')
            """
        )
        conn.execute(
            """
            CREATE TABLE strfieldenummodel (
                id BIGSERIAL PRIMARY KEY,
                status strfieldrowstatus NOT NULL
            )
            """
        )

    await connect(db_url, auto_migrate=False)

    row = await StrFieldEnumModel.create(status="active")
    fetched = await StrFieldEnumModel.get(row.id)

    assert fetched is not None
    assert fetched.status == "active"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_uuid_null_inserts(
    db_url, postgres_base_url, db_schema_name
):
    """Optional ``UUID`` columns on Postgres need ``::uuid`` casts, including for ``NULL``."""

    class UuidRun(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        run_id: uuid.UUID | None = None

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute(
            """
            CREATE TABLE uuidrun (
                id BIGSERIAL PRIMARY KEY,
                run_id uuid
            )
            """
        )
        conn.commit()

    await connect(db_url, auto_migrate=False)

    row = await UuidRun.create()
    assert row.id is not None
    assert row.run_id is None
    u = uuid.uuid4()
    row2 = await UuidRun.create(run_id=u)
    f2 = await UuidRun.get(row2.id)
    assert f2 is not None
    assert f2.run_id == u


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_timestamp_without_time_zone_null_and_value(
    db_url, postgres_base_url, db_schema_name
):
    """``datetime | None`` on ``timestamp without time zone`` must not bind as plain text."""

    from datetime import UTC, datetime

    class RowWithTs(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        scrubbed_at: datetime | None = None

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute(
            """
            CREATE TABLE rowwithts (
                id BIGSERIAL PRIMARY KEY,
                scrubbed_at timestamp without time zone
            )
            """
        )
        conn.commit()

    await connect(db_url, auto_migrate=False)

    row = await RowWithTs.create()
    assert row.scrubbed_at is None
    d = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    row2 = await RowWithTs.create(scrubbed_at=d)
    f2 = await RowWithTs.get(row2.id)
    assert f2 is not None
    assert f2.scrubbed_at is not None
