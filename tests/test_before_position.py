"""``before(position)`` previous-page paging on root order keys (#395, ADR-0018).

Build-time tests need no database. E2e tests run on the backend matrix.
"""

from datetime import UTC, datetime
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model
from ferro.query.wire import compile_query


class BeforePageItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    updated_at: datetime
    name: str


def _ordered(item: BeforePageItem):
    return (
        BeforePageItem.select()
        .order_by(lambda row: row.updated_at)
        .order_by(lambda row: row.id)
    )


# ---------------------------------------------------------------------------
# Build-time (no DB).
# ---------------------------------------------------------------------------


def test_before_without_pk_in_order_keys_raises():
    with pytest.raises(ValueError, match=r"primary key"):
        BeforePageItem.select().order_by(lambda row: row.updated_at).before(
            (datetime(2026, 1, 1, tzinfo=UTC),)
        )


def test_before_on_pkless_model_raises():
    class BeforePkLess(Model):
        name: str
        rank: int = 0

    with pytest.raises(ValueError, match=r"BeforePkLess.*no primary-key"):
        BeforePkLess.select().order_by(lambda row: row.rank).order_by(
            lambda row: row.name
        ).before((1, "x"))


def test_before_none_in_non_pk_slot_is_legal():
    class BeforeNullableNone(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str | None = None

    query = (
        BeforeNullableNone.select()
        .order_by(lambda row: row.label)
        .order_by(lambda row: row.id)
        .before((None, 1))
    )
    assert query._before == (None, 1)


def test_before_none_on_not_null_root_column_is_legal():
    query = _ordered(BeforePageItem).before((None, 1))
    assert query._before == (None, 1)


def test_before_wrong_arity_raises():
    with pytest.raises(ValueError, match=r"arity|values"):
        _ordered(BeforePageItem).before((datetime(2026, 1, 1, tzinfo=UTC),))


def test_before_none_in_pk_slot_raises():
    with pytest.raises(ValueError, match=r"primary-key|primary key"):
        _ordered(BeforePageItem).before((datetime(2026, 1, 1, tzinfo=UTC), None))


def test_before_plus_offset_raises():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"offset"):
        _ordered(BeforePageItem).offset(1).before((ts, 1))
    with pytest.raises(ValueError, match=r"offset"):
        _ordered(BeforePageItem).before((ts, 1)).offset(1)


def test_after_plus_before_raises():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"after|before"):
        _ordered(BeforePageItem).after((ts, 1)).before((ts, 2))
    with pytest.raises(ValueError, match=r"after|before"):
        _ordered(BeforePageItem).before((ts, 2)).after((ts, 1))


def test_before_is_immutable():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    base = _ordered(BeforePageItem)
    paged = base.before((ts, 1)).limit(2)
    assert paged is not base
    assert base._before is None
    assert paged._before == (ts, 1)
    assert paged._limit == 2
    assert base._limit is None


def test_update_and_delete_reject_before():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    query = _ordered(BeforePageItem).before((ts, 1))
    with pytest.raises(ValueError, match=r"before"):
        compile_query(query, "update", assignments={"name": "x"})
    with pytest.raises(ValueError, match=r"before"):
        compile_query(query, "delete")


def test_count_drops_before_from_the_wire():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    query = _ordered(BeforePageItem).before((ts, 1)).limit(3)
    payload = compile_query(query, "count").payload.to_ir_dict()
    assert "before" not in payload
    assert payload["limit"] is None
    assert payload["offset"] is None


def test_fetch_omits_before_when_unset():
    payload = compile_query(_ordered(BeforePageItem), "fetch").payload.to_ir_dict()
    assert "before" not in payload
    assert "after" not in payload


# ---------------------------------------------------------------------------
# E2e: adjacent previous page in declared order.
# ---------------------------------------------------------------------------


async def _seed_page_items() -> list[BeforePageItem]:
    rows = [
        BeforePageItem(id=1, updated_at=datetime(2026, 1, 1, tzinfo=UTC), name="a"),
        BeforePageItem(id=2, updated_at=datetime(2026, 1, 1, tzinfo=UTC), name="b"),
        BeforePageItem(id=3, updated_at=datetime(2026, 2, 1, tzinfo=UTC), name="c"),
        BeforePageItem(id=4, updated_at=datetime(2026, 3, 1, tzinfo=UTC), name="d"),
        BeforePageItem(id=5, updated_at=datetime(2026, 3, 1, tzinfo=UTC), name="e"),
    ]
    for row in rows:
        await row.save()
    return rows


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_limit_returns_adjacent_previous_page_in_declared_order(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        ordered = await _ordered(BeforePageItem).all()
        assert [row.id for row in ordered] == [1, 2, 3, 4, 5]

        page = (
            await _ordered(BeforePageItem)
            .before((ordered[3].updated_at, ordered[3].id))
            .limit(2)
            .all()
        )
        assert [row.id for row in page] == [2, 3]
        assert ordered[3] not in page

        past_start = (
            await _ordered(BeforePageItem)
            .before((ordered[0].updated_at, ordered[0].id))
            .limit(2)
            .all()
        )
        assert past_start == []

        assert (
            await _ordered(BeforePageItem)
            .before((ordered[3].updated_at, ordered[3].id))
            .count()
            == 5
        )


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_unbounded_before_returns_every_earlier_row_in_declared_order(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        ordered = await _ordered(BeforePageItem).all()
        prefix = await _ordered(BeforePageItem).before(
            (ordered[3].updated_at, ordered[3].id)
        ).all()
        assert [row.id for row in prefix] == [1, 2, 3]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_first_is_adjacent_previous_row(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        ordered = await _ordered(BeforePageItem).all()
        adjacent = await _ordered(BeforePageItem).before(
            (ordered[3].updated_at, ordered[3].id)
        ).first()
        assert adjacent is not None
        assert adjacent.id == 3


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_unbounded_before_first_disagrees_with_all_head(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        ordered = await _ordered(BeforePageItem).all()
        query = _ordered(BeforePageItem).before((ordered[3].updated_at, ordered[3].id))
        prefix = await query.all()
        adjacent = await query.first()
        assert [row.id for row in prefix] == [1, 2, 3]
        assert prefix[0].id == 1
        assert adjacent is not None
        assert adjacent.id == 3
        assert adjacent.id != prefix[0].id


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_row_equals_before_tuple(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        query = _ordered(BeforePageItem)
        rows = await query.all()
        anchor = rows[3]
        position = query.position_of(anchor)
        assert position == (anchor.updated_at, anchor.id)

        from_tuple = await query.before(position).limit(2).all()
        from_row = await query.before(anchor).limit(2).all()
        assert [row.id for row in from_tuple] == [2, 3]
        assert [row.id for row in from_row] == [2, 3]


# ---------------------------------------------------------------------------
# E2e: NULL-bucket crossing (Pinch shape — pinned DESC last, then PK).
# ---------------------------------------------------------------------------


class BeforePinchConvo(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    pinned_at: datetime | None = None
    title: str


def _pinch(model: type[BeforePinchConvo] = BeforePinchConvo):
    return (
        model.select()
        .order_by(lambda convo: convo.pinned_at, "desc")
        .order_by(lambda convo: convo.id)
    )


async def _seed_pinch_convos() -> list[BeforePinchConvo]:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, tzinfo=UTC)
    rows = [
        BeforePinchConvo(id=1, pinned_at=t3, title="pinned-latest"),
        BeforePinchConvo(id=2, pinned_at=t2, title="pinned-mid"),
        BeforePinchConvo(id=3, pinned_at=t1, title="pinned-oldest"),
        BeforePinchConvo(id=4, pinned_at=None, title="unpinned-a"),
        BeforePinchConvo(id=5, pinned_at=None, title="unpinned-b"),
        BeforePinchConvo(id=6, pinned_at=None, title="unpinned-c"),
    ]
    for row in rows:
        await row.save()
    return rows


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_crosses_null_bucket_into_pinned(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_pinch_convos()
        ordered = await _pinch().all()
        assert [row.id for row in ordered] == [1, 2, 3, 4, 5, 6]

        first_unpinned = ordered[3]
        assert first_unpinned.pinned_at is None
        adjacent = await _pinch().before((None, first_unpinned.id)).limit(1).all()
        assert [row.id for row in adjacent] == [3]

        page = await _pinch().before((None, first_unpinned.id)).limit(2).all()
        assert [row.id for row in page] == [2, 3]

        prefix = await _pinch().before((None, first_unpinned.id)).all()
        assert [row.id for row in prefix] == [1, 2, 3]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_null_bound_stays_in_null_bucket_when_later(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_pinch_convos()
        earlier_unpinned = await _pinch().before((None, 6)).limit(2).all()
        assert [row.id for row in earlier_unpinned] == [4, 5]
