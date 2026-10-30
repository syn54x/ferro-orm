"""A JSON-family field hydrates as its declared type, not the decoded JSON (#737).

The JSON decoder produces ``dict``, ``list``, ``str``, numbers, ``bool`` and
``None``. A field declared as a nested model, ``set``, ``tuple``,
``datetime``, ``UUID`` and the like therefore needs its decoded value
validated through the field's Pydantic type adapter, the validation
``__init__`` runs (I-2). A field the decoder already produces keeps the
zero-copy path and has no converter.
"""

import warnings
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import BaseModel

import ferro
from ferro import FerroField, Model
from ferro.models import _hydrated_returning
from ferro.raw import execute

pytestmark = pytest.mark.backend_matrix


class Dimensions(BaseModel):
    width_mm: int
    height_mm: int


class Tone(StrEnum):
    WARM = "warm"
    COOL = "cool"


class JShade(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    dimensions: Dimensions | None = None
    items: list[Dimensions] = []


class JRich(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    tags: set[str] = set()
    pair: tuple[int, int] = (0, 0)
    stamps: list[datetime] = []
    owners: dict[str, UUID] = {}
    prices: list[Decimal] = []
    tones: dict[str, Tone] = {}


class JNative(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    counts: dict[str, int] = {}
    names: list[str] = []
    blob: dict = {}


STAMP = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
OWNER = UUID("12345678-1234-5678-1234-567812345678")


def _rich() -> JRich:
    return JRich(
        id=1,
        tags={"a", "b"},
        pair=(3, 4),
        stamps=[STAMP],
        owners={"x": OWNER},
        prices=[Decimal("1.50")],
        tones={"k": Tone.WARM},
    )


def test_native_json_fields_register_no_converter():
    assert JNative._hydration_converters == {}


def test_declared_json_fields_register_a_json_converter():
    kinds = {n: kind for n, (kind, _) in JShade._hydration_converters.items()}
    assert kinds == {"dimensions": "json", "items": "json"}
    assert set(JRich._hydration_converters) == {
        "tags",
        "pair",
        "stamps",
        "owners",
        "prices",
        "tones",
    }


@pytest.mark.asyncio
async def test_nested_model_hydrates_as_the_model(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await JShade(
            id=1,
            dimensions=Dimensions(width_mm=300, height_mm=220),
            items=[Dimensions(width_mm=1, height_mm=1)],
        ).save()
    async with ferro.engines.session():
        fetched = await JShade.get(1)
    assert isinstance(fetched.dimensions, Dimensions)
    assert fetched.dimensions == Dimensions(width_mm=300, height_mm=220)
    assert fetched.items == [Dimensions(width_mm=1, height_mm=1)]
    assert isinstance(fetched.items[0], Dimensions)


@pytest.mark.asyncio
async def test_rich_types_hydrate_as_constructed(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    built = _rich()
    async with ferro.engines.session():
        await built.save()
    async with ferro.engines.session():
        fetched = await JRich.get(1)
    for name in ("tags", "pair", "stamps", "owners", "prices", "tones"):
        assert getattr(fetched, name) == getattr(built, name), name
        assert type(getattr(fetched, name)) is type(getattr(built, name)), name
    assert type(fetched.owners["x"]) is UUID
    assert type(fetched.stamps[0]) is datetime
    assert type(fetched.prices[0]) is Decimal
    assert type(fetched.tones["k"]) is Tone
    # No serializer warning: the values are the declared types.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fetched.model_dump_json()


@pytest.mark.asyncio
async def test_null_optional_model_stays_none(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await JShade(id=1).save()
    async with ferro.engines.session():
        fetched = await JShade.get(1)
    assert fetched.dimensions is None
    assert fetched.items == []


@pytest.mark.asyncio
async def test_native_json_values_are_unchanged(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await JNative(id=1, counts={"a": 1}, names=["x"], blob={"k": [1, "v"]}).save()
    async with ferro.engines.session():
        fetched = await JNative.get(1)
    assert fetched.counts == {"a": 1}
    assert fetched.names == ["x"]
    assert fetched.blob == {"k": [1, "v"]}


@pytest.mark.asyncio
async def test_row_projection_hydrates_declared_type(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await JShade(id=1, dimensions=Dimensions(width_mm=3, height_mm=4)).save()
        rows = await JShade.select(lambda jshade: (jshade.id, jshade.dimensions)).all()
    assert isinstance(rows[0].dimensions, Dimensions)
    assert rows[0].dimensions == Dimensions(width_mm=3, height_mm=4)


@pytest.mark.asyncio
async def test_same_session_refresh_keeps_declared_type(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        held = await JShade.create(id=1, dimensions=Dimensions(width_mm=3, height_mm=4))
        got = await JShade.get(1)
        again = await JShade.where(lambda jshade: jshade.id == 1).first()
        assert got is held is again
        assert isinstance(held.dimensions, Dimensions)
        assert held.dimensions == Dimensions(width_mm=3, height_mm=4)


def test_returning_values_take_the_json_converter():
    returned = _hydrated_returning(
        JShade, {"dimensions": {"width_mm": 1, "height_mm": 2}, "items": []}
    )
    assert returned["dimensions"] == Dimensions(width_mm=1, height_mm=2)
    assert returned["items"] == []


@pytest.mark.asyncio
async def test_rejected_document_names_json_model_and_field(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await JShade(id=1).save()
        await execute("UPDATE jshade SET dimensions = '{\"width_mm\": 1}' WHERE id = 1")
    async with ferro.engines.session():
        with pytest.raises(
            ValueError, match=r"Failed to hydrate json field JShade\.dimensions"
        ):
            await JShade.get(1)
