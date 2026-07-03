"""Cross-backend hydration-equivalence sentinels (FF-C C3/C4).

These tests pin the hydrated Python value for every rich column type against
fixed expected constants, on every backend in the matrix. Because both
backends are asserted against the *same* constants, cross-backend equivalence
follows transitively: if SQLite and Postgres each hydrate exactly the expected
value, they hydrate equal values.

The suite was written BEFORE the C3 native-decode swap (per the roadmap risk
register) and passed on the text-decode path, so it pins behavior across the
swap: native decode must produce byte-for-byte the same Python values the
`CAST(... AS text)` round-trip produced.

Tests marked ``xfail(strict=True)`` are the deliberately-red TDD targets for
C3 — behaviors the text path gets wrong (``time`` hydrating as ``str``,
``timestamptz`` values coupled to the session ``TimeZone``). The swap commit
flips them green by removing the markers' reason for existing.

The suite connects with ``identity_map=False`` so every fetch hydrates from
the wire instead of returning the instance that was saved.
"""

import datetime
import decimal
import enum
import uuid
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model, clear_registry, reset_engine

pytestmark = pytest.mark.backend_matrix

UTC = datetime.timezone.utc

AWARE_AT = datetime.datetime(2024, 3, 15, 13, 45, 30, 123456, tzinfo=UTC)
NAIVE_AT = datetime.datetime(2024, 3, 15, 13, 45, 30, 123456)
DAY = datetime.date(2024, 3, 15)
OPENS_AT = datetime.time(9, 30, 15, 250000)
TOKEN = uuid.UUID("0192aef3-4c92-7d21-a3ee-cdd8ab21b1c4")
AMOUNT = decimal.Decimal("123.45")
PAYLOAD = {"theme": "dark", "levels": [1, 2, 3], "nested": {"ok": True}}
DATA = b"ferro-bytes"
RATIO = 2.5


class Role(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()


def _build_specimen() -> type[Model]:
    class Specimen(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        aware_at: datetime.datetime  # timestamptz
        naive_at: Annotated[datetime.datetime, FerroField(db_type="timestamp")]
        day: datetime.date
        opens_at: datetime.time
        token: uuid.UUID
        amount: decimal.Decimal
        payload: dict
        data: bytes
        role: Role  # native PG enum UDT / text on SQLite
        priority: Priority  # int-family storage
        ratio: float
        active: bool
        name: str
        note: str | None = None

    return Specimen


def _make_specimen(cls: type[Model]) -> Model:
    return cls(
        aware_at=AWARE_AT,
        naive_at=NAIVE_AT,
        day=DAY,
        opens_at=OPENS_AT,
        token=TOKEN,
        amount=AMOUNT,
        payload=PAYLOAD,
        data=DATA,
        role=Role.ADMIN,
        priority=Priority.HIGH,
        ratio=RATIO,
        active=True,
        name="specimen",
    )


async def _connect_and_save(db_url: str) -> tuple[type[Model], int]:
    specimen_cls = _build_specimen()
    await ferro.connect(db_url, auto_migrate=True, identity_map=False)
    row = _make_specimen(specimen_cls)
    await row.save()
    assert row.id is not None
    return specimen_cls, row.id


def _assert_core_values(row: Model) -> None:
    """The equivalence contract, minus the deliberately-red C3 shapes."""
    assert isinstance(row.aware_at, datetime.datetime)
    assert row.aware_at.tzinfo is not None, "timestamptz must hydrate tz-aware"
    assert row.aware_at == AWARE_AT

    assert isinstance(row.naive_at, datetime.datetime)
    assert row.naive_at.tzinfo is None, "timestamp (naive) must hydrate naive"
    assert row.naive_at == NAIVE_AT

    assert type(row.day) is datetime.date
    assert row.day == DAY

    assert isinstance(row.token, uuid.UUID)
    assert row.token == TOKEN

    assert isinstance(row.amount, decimal.Decimal)
    assert row.amount == AMOUNT

    assert row.payload == PAYLOAD
    assert isinstance(row.data, bytes)
    assert row.data == DATA

    assert isinstance(row.role, Role)
    assert row.role is Role.ADMIN

    assert isinstance(row.ratio, float)
    assert row.ratio == RATIO
    assert row.active is True
    assert row.name == "specimen"
    assert row.note is None


@pytest.mark.asyncio
async def test_full_type_row_hydrates_expected_values(db_url):
    specimen_cls, _pk = await _connect_and_save(db_url)

    rows = await specimen_cls.all()
    assert len(rows) == 1
    _assert_core_values(rows[0])


@pytest.mark.asyncio
async def test_every_fetch_path_hydrates_identically(db_url):
    """all(), get(), where().all(), first(), get_or_none(), refresh() agree."""
    specimen_cls, pk = await _connect_and_save(db_url)

    fetched = [
        (await specimen_cls.all())[0],
        await specimen_cls.get(pk),
        (await specimen_cls.where(lambda s: s.name == "specimen").all())[0],
        await specimen_cls.where(lambda s: s.token == TOKEN).first(),
        await specimen_cls.get_or_none(pk),
    ]

    refreshed = await specimen_cls.get(pk)
    await refreshed.refresh()
    fetched.append(refreshed)

    for row in fetched:
        _assert_core_values(row)


@pytest.mark.asyncio
async def test_uuid_exact_object_equality_not_str(db_url):
    """UUID casing must not leak: compare UUID objects and canonical str."""
    specimen_cls, pk = await _connect_and_save(db_url)
    row = await specimen_cls.get(pk)
    assert row.token == TOKEN
    assert str(row.token) == str(TOKEN)  # canonical lowercase hyphenated


@pytest.mark.asyncio
async def test_decimal_survives_filter_roundtrip(db_url):
    specimen_cls, _pk = await _connect_and_save(db_url)
    row = await specimen_cls.where(lambda s: s.amount == AMOUNT).first()
    assert row is not None
    assert row.amount == AMOUNT


# --------------------------------------------------------------------------
# Deliberately-red TDD targets for the C3 native-decode swap.
# xfail(strict=True): they MUST fail on the text-decode path; the swap commit
# removes the markers and they become part of the standing contract.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_hydrates_datetime_time(db_url):
    specimen_cls, pk = await _connect_and_save(db_url)
    row = await specimen_cls.get(pk)
    assert type(row.opens_at) is datetime.time
    assert row.opens_at == OPENS_AT


@pytest.mark.asyncio
async def test_int_enum_hydrates_member(db_url):
    specimen_cls, pk = await _connect_and_save(db_url)
    row = await specimen_cls.get(pk)
    assert isinstance(row.priority, Priority)
    assert row.priority is Priority.HIGH


@pytest.mark.asyncio
@pytest.mark.postgres_only
@pytest.mark.xfail(
    strict=True,
    reason="C3 red: text-decode renders timestamptz in the session TimeZone",
)
async def test_timestamptz_stable_under_non_utc_session_timezone(db_url):
    """Native decode must yield the same aware UTC datetime no matter what
    the session TimeZone renders text as.

    A 1-connection pool makes ``SET TIME ZONE`` stick for every subsequent
    statement (sqlx 0.8 silently drops the ``options`` startup URL param, so
    the session GUC route is the reliable one).
    """
    from ferro.raw import execute

    specimen_cls = _build_specimen()
    await ferro.connect(
        db_url,
        auto_migrate=True,
        identity_map=False,
        pool=ferro.PoolConfig(max_connections=1),
    )
    await execute("SET TIME ZONE 'America/Chicago'")
    assert (await ferro.raw.fetch_one("SHOW timezone"))["TimeZone"] == "America/Chicago"

    row = _make_specimen(specimen_cls)
    await row.save()
    pk = row.id
    row = await specimen_cls.get(pk)

    assert row.aware_at == AWARE_AT
    assert row.aware_at.utcoffset() == datetime.timedelta(0), (
        "hydrated timestamptz must be stable UTC regardless of session TimeZone, "
        f"got offset {row.aware_at.utcoffset()!r}"
    )
