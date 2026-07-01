"""#162 — typed save/update value bind: binary round-trips + parity."""

from __future__ import annotations

import datetime
import decimal
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import FerroField, Model

pytestmark = pytest.mark.backend_matrix

# Shared across save/update/bulk tasks. Each must round-trip byte-for-byte.
BINARY_VECTORS = [
    b"\x89PNG\r\n\x1a\n\xff\xd8",  # PNG magic — non-UTF-8
    b"%PDF-1.7\x00\xff\xfe",        # PDF-ish — non-UTF-8
    bytes(range(256)),              # every byte value
    b"hello",                       # UTF-8 (must stay real bytes, not text coercion)
    b"",                            # empty
]


@pytest.mark.parametrize(
    "payload",
    BINARY_VECTORS,
    ids=["png-magic", "pdf-magic", "all-bytes", "utf8-bytes", "empty"],
)
@pytest.mark.asyncio
async def test_save_roundtrips_binary(db_url, payload):
    class SaveDoc(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        data: bytes = b""

    await ferro.connect(db_url, auto_migrate=True)

    doc = SaveDoc(id=uuid4(), data=payload)
    await doc.save()
    got = (await SaveDoc.all())[0].data
    assert isinstance(got, bytes)
    assert got == payload


@pytest.mark.asyncio
async def test_save_nullable_bytes_roundtrips_none(db_url):
    class SaveNullDoc(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        blob: bytes | None = None

    await ferro.connect(db_url, auto_migrate=True)

    doc = SaveNullDoc(id=uuid4(), blob=None)
    await doc.save()
    assert (await SaveNullDoc.all())[0].blob is None


@pytest.mark.asyncio
async def test_save_preserves_rich_types(db_url):
    class SaveRec(Model):
        id: Annotated[UUID | None, FerroField(primary_key=True)] = None
        uid: UUID
        when: datetime.datetime
        amount: decimal.Decimal
        count: int
        active: bool
        tags: list[str] = []
        note: str | None = None

    await ferro.connect(db_url, auto_migrate=True)

    uid = uuid4()
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    amount = decimal.Decimal("12.34")
    rec = SaveRec(
        id=uuid4(), uid=uid, when=when, amount=amount,
        count=7, active=True, tags=["a", "b"],
    )
    await rec.save()

    got = (await SaveRec.all())[0]
    assert got.uid == uid
    assert got.amount == amount
    assert got.count == 7
    assert got.active is True
    assert got.tags == ["a", "b"]
    assert got.note is None
    assert got.when == when
