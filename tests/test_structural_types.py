import pytest
import uuid
import psycopg
from decimal import Decimal
from enum import Enum
from typing import Annotated, Dict, List
from ferro import Model, connect, FerroField

pytestmark = pytest.mark.backend_matrix


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"


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
async def test_optional_uuid_roundtrip_with_null(db_url):
    """Optional UUID columns should persist NULL cleanly on every backend."""

    class OptionalUuidModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        scorecard_flow_run_id: uuid.UUID | None = None

    await connect(db_url, auto_migrate=True)

    row = await OptionalUuidModel.create(name="job-role")
    fetched = await OptionalUuidModel.get(row.id)

    assert fetched is not None
    assert fetched.scorecard_flow_run_id is None


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_native_postgres_enum_roundtrip(
    db_url, postgres_base_url, db_schema_name
):
    """Native Postgres enum columns should accept enum-backed string values."""

    class NativeUserStatus(str, Enum):
        ACTIVE = "active"
        INACTIVE = "inactive"

    class NativeEnumModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: NativeUserStatus

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        conn.execute(
            """
            CREATE TYPE nativeuserstatus AS ENUM ('active', 'inactive')
            """
        )
        conn.execute(
            """
            CREATE TABLE nativeenummodel (
                id BIGSERIAL PRIMARY KEY,
                status nativeuserstatus NOT NULL
            )
            """
        )

    await connect(db_url, auto_migrate=False)

    row = await NativeEnumModel.create(status=NativeUserStatus.ACTIVE)
    fetched = await NativeEnumModel.get(row.id)

    assert fetched is not None
    assert fetched.status == NativeUserStatus.ACTIVE
