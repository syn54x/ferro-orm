"""bulk_create internal chunking under backend bind-parameter limits (#298).

All tests exercise the public seam — ``Model.bulk_create`` over the backend
matrix. Chunking is an implementation detail: the only observable contract is
that any batch size succeeds, the returned count is the total inserted, and
atomicity is preserved with and without an ambient transaction.

Bind budget arithmetic used throughout: ``ChunkedItem`` renders 5 columns per
row (the unset autoincrement pk is skipped), so 13,200 rows bind 66,000
parameters — over sqlite's 32,766 (SQLITE_MAX_VARIABLE_NUMBER) and Postgres's
65,535 (int16 parameter count in the wire protocol's Bind message).
"""

import pytest
from typing import Annotated

from ferro import FerroField, Model, connect, engines, transaction
from ferro.exceptions import IntegrityError

pytestmark = pytest.mark.backend_matrix

STRADDLE_ROWS = 13_200  # 13,200 rows x 5 columns = 66,000 binds > both limits


class ChunkedItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    quantity: int
    price: float
    is_active: bool
    note: str


def make_items(n: int) -> list[ChunkedItem]:
    return [
        ChunkedItem(
            label=f"item-{i}",
            quantity=i,
            price=i * 0.5,
            is_active=i % 2 == 0,
            note=f"note-{i}",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_bulk_create_succeeds_beyond_bind_limits(db_url):
    """A batch binding more parameters than either backend allows inserts fully."""

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        inserted = await ChunkedItem.bulk_create(make_items(STRADDLE_ROWS))
        assert inserted == STRADDLE_ROWS

        assert (
            await ChunkedItem.where(lambda item: item.quantity >= 0).count()
            == STRADDLE_ROWS
        )

        # Spot-check a row from the final chunk survived with its values intact.
        last = await ChunkedItem.where(
            lambda item: item.label == f"item-{STRADDLE_ROWS - 1}"
        ).first()
        assert last is not None
        assert last.quantity == STRADDLE_ROWS - 1
        assert last.note == f"note-{STRADDLE_ROWS - 1}"


class UniqueChunkedItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: Annotated[str, FerroField(unique=True)]
    quantity: int
    price: float
    is_active: bool
    note: str


def make_unique_items(n: int) -> list[UniqueChunkedItem]:
    return [
        UniqueChunkedItem(
            label=f"item-{i}",
            quantity=i,
            price=i * 0.5,
            is_active=i % 2 == 0,
            note=f"note-{i}",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_bare_bulk_create_is_all_or_nothing_across_chunks(db_url):
    """A late-chunk failure in a bare call must leave no rows from the batch.

    The collision sits at index 13,150 — past every chunk boundary either
    backend produces for a 5-column row budget — so earlier chunks have
    already executed when the violation fires.
    """

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await UniqueChunkedItem.create(
            label="item-13150", quantity=0, price=0.0, is_active=True, note="seed"
        )

        with pytest.raises(IntegrityError):
            await UniqueChunkedItem.bulk_create(make_unique_items(STRADDLE_ROWS))

        survivors = await UniqueChunkedItem.all()
        assert len(survivors) == 1
        assert survivors[0].note == "seed"


@pytest.mark.asyncio
async def test_ambient_transaction_is_the_atomicity_boundary(db_url):
    """An aborted ambient transaction() rolls back every internal chunk."""

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        try:
            async with transaction():
                inserted = await ChunkedItem.bulk_create(make_items(STRADDLE_ROWS))
                assert inserted == STRADDLE_ROWS
                raise RuntimeError("abort after chunked bulk_create")
        except RuntimeError:
            pass

        assert await ChunkedItem.where(lambda item: item.quantity >= 0).count() == 0


@pytest.mark.asyncio
async def test_late_chunk_failure_inside_ambient_transaction_leaves_zero_rows(db_url):
    """A mid-batch unique violation propagates and aborts the ambient transaction."""

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        with pytest.raises(IntegrityError):
            async with transaction():
                await UniqueChunkedItem.create(
                    label="item-13150",
                    quantity=0,
                    price=0.0,
                    is_active=True,
                    note="seed",
                )
                await UniqueChunkedItem.bulk_create(make_unique_items(STRADDLE_ROWS))

        assert await UniqueChunkedItem.all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        6_553,  # 32,765 binds — one under sqlite's 32,766 ceiling
        6_554,  # 32,770 binds — the 0.16.0 sqlite failure threshold (#298)
        13_107,  # 65,535 binds — exactly Postgres's ceiling
    ],
)
async def test_bulk_create_at_exact_bind_limit_boundaries(db_url, rows):
    """Row counts straddling each backend's exact bind ceiling insert fully."""

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        assert await ChunkedItem.bulk_create(make_items(rows)) == rows
        assert await ChunkedItem.where(lambda item: item.quantity >= 0).count() == rows
