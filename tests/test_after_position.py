"""``after(position)`` on root order keys, including NULL buckets (#393/#394, ADR-0018).

Build-time tests need no database. E2e tests run on the backend matrix.
"""

from datetime import UTC, datetime
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model
from ferro.query.wire import compile_query


class AfterPageItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    updated_at: datetime
    name: str


def _ordered(item: AfterPageItem):
    return (
        AfterPageItem.select()
        .order_by(lambda row: row.updated_at)
        .order_by(lambda row: row.id)
    )


# ---------------------------------------------------------------------------
# Build-time (no DB).
# ---------------------------------------------------------------------------


def test_after_without_pk_in_order_keys_raises():
    with pytest.raises(ValueError, match=r"primary key"):
        AfterPageItem.select().order_by(lambda row: row.updated_at).after(
            (datetime(2026, 1, 1, tzinfo=UTC),)
        )


def test_after_on_pkless_model_raises():
    class AfterPkLess(Model):
        name: str
        rank: int = 0

    with pytest.raises(ValueError, match=r"AfterPkLess.*no primary-key"):
        AfterPkLess.select().order_by(lambda row: row.rank).order_by(
            lambda row: row.name
        ).after((1, "x"))


def test_position_of_on_pkless_model_raises():
    class AfterPkLessPos(Model):
        name: str

    row = AfterPkLessPos(name="x")
    with pytest.raises(ValueError, match=r"AfterPkLessPos.*no primary-key"):
        AfterPkLessPos.select().order_by(lambda r: r.name).position_of(row)


def test_after_accepts_nullable_order_key():
    class AfterNullableKey(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str | None = None

    query = (
        AfterNullableKey.select()
        .order_by(lambda row: row.label)
        .order_by(lambda row: row.id)
        .after(("x", 1))
    )
    assert query._after == ("x", 1)


def test_after_none_in_non_pk_slot_is_legal():
    class AfterNullableNone(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str | None = None

    query = (
        AfterNullableNone.select()
        .order_by(lambda row: row.label)
        .order_by(lambda row: row.id)
        .after((None, 1))
    )
    assert query._after == (None, 1)


def test_after_none_on_not_null_root_column_is_legal():
    # A NOT NULL column can still be a NULL position slot (left_join missing
    # relation — traversal e2e is #396). Nullability is not consulted.
    query = _ordered(AfterPageItem).after((None, 1))
    assert query._after == (None, 1)


def test_position_of_reads_none_in_non_pk_slot():
    class AfterNullablePos(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str | None = None

    row = AfterNullablePos(id=7, label=None)
    position = (
        AfterNullablePos.select()
        .order_by(lambda row: row.label)
        .order_by(lambda row: row.id)
        .position_of(row)
    )
    assert position == (None, 7)


def test_after_wrong_arity_raises():
    with pytest.raises(ValueError, match=r"arity|values"):
        _ordered(AfterPageItem).after((datetime(2026, 1, 1, tzinfo=UTC),))


def test_after_none_in_pk_slot_raises():
    with pytest.raises(ValueError, match=r"primary-key|primary key"):
        _ordered(AfterPageItem).after((datetime(2026, 1, 1, tzinfo=UTC), None))


def test_after_plus_offset_raises():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"offset"):
        _ordered(AfterPageItem).offset(1).after((ts, 1))
    with pytest.raises(ValueError, match=r"offset"):
        _ordered(AfterPageItem).after((ts, 1)).offset(1)


def test_after_is_immutable():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    base = _ordered(AfterPageItem)
    paged = base.after((ts, 1)).limit(2)
    assert paged is not base
    assert base._after is None
    assert paged._after == (ts, 1)
    assert paged._limit == 2
    assert base._limit is None


def test_update_and_delete_reject_after():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    query = _ordered(AfterPageItem).after((ts, 1))
    with pytest.raises(ValueError, match=r"after"):
        compile_query(query, "update", assignments={"name": "x"})
    with pytest.raises(ValueError, match=r"after"):
        compile_query(query, "delete")


def test_count_drops_after_from_the_wire():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    query = _ordered(AfterPageItem).after((ts, 1)).limit(3)
    payload = compile_query(query, "count").payload.to_ir_dict()
    assert "after" not in payload
    assert payload["limit"] is None
    assert payload["offset"] is None


# ---------------------------------------------------------------------------
# E2e: exclusive next page in declared order.
# ---------------------------------------------------------------------------


async def _seed_page_items() -> list[AfterPageItem]:
    rows = [
        AfterPageItem(id=1, updated_at=datetime(2026, 1, 1, tzinfo=UTC), name="a"),
        AfterPageItem(id=2, updated_at=datetime(2026, 1, 1, tzinfo=UTC), name="b"),
        AfterPageItem(id=3, updated_at=datetime(2026, 2, 1, tzinfo=UTC), name="c"),
        AfterPageItem(id=4, updated_at=datetime(2026, 3, 1, tzinfo=UTC), name="d"),
        AfterPageItem(id=5, updated_at=datetime(2026, 3, 1, tzinfo=UTC), name="e"),
    ]
    for row in rows:
        await row.save()
    return rows


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_after_returns_next_n_exclusive_in_declared_order(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        ordered = await _ordered(AfterPageItem).all()
        assert [row.id for row in ordered] == [1, 2, 3, 4, 5]

        page = (
            await _ordered(AfterPageItem)
            .after((ordered[1].updated_at, ordered[1].id))
            .limit(2)
            .all()
        )
        assert [row.id for row in page] == [3, 4]
        assert ordered[1] not in page

        past_end = (
            await _ordered(AfterPageItem)
            .after((ordered[-1].updated_at, ordered[-1].id))
            .limit(2)
            .all()
        )
        assert past_end == []

        # count() drops paging the same way it drops limit/offset.
        assert (
            await _ordered(AfterPageItem)
            .after((ordered[1].updated_at, ordered[1].id))
            .count()
            == 5
        )


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_position_of_matches_tuple_and_after_row_equals_after_tuple(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_page_items()
        query = _ordered(AfterPageItem)
        rows = await query.all()
        anchor = rows[1]
        position = query.position_of(anchor)
        assert position == (anchor.updated_at, anchor.id)

        from_tuple = await query.after(position).limit(2).all()
        from_row = await query.after(anchor).limit(2).all()
        assert [row.id for row in from_tuple] == [3, 4]
        assert [row.id for row in from_row] == [3, 4]


# ---------------------------------------------------------------------------
# E2e: NULL-bucket crossing (Pinch shape — pinned DESC last, then PK).
# ---------------------------------------------------------------------------


class AfterPinchConvo(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    pinned_at: datetime | None = None
    title: str


def _pinch(model: type[AfterPinchConvo] = AfterPinchConvo):
    return (
        model.select()
        .order_by(lambda convo: convo.pinned_at, "desc")
        .order_by(lambda convo: convo.id)
    )


async def _seed_pinch_convos() -> list[AfterPinchConvo]:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, tzinfo=UTC)
    rows = [
        AfterPinchConvo(id=1, pinned_at=t3, title="pinned-latest"),
        AfterPinchConvo(id=2, pinned_at=t2, title="pinned-mid"),
        AfterPinchConvo(id=3, pinned_at=t1, title="pinned-oldest"),
        AfterPinchConvo(id=4, pinned_at=None, title="unpinned-a"),
        AfterPinchConvo(id=5, pinned_at=None, title="unpinned-b"),
        AfterPinchConvo(id=6, pinned_at=None, title="unpinned-c"),
    ]
    for row in rows:
        await row.save()
    return rows


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_after_none_continues_through_null_bucket(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_pinch_convos()
        ordered = await _pinch().all()
        assert [row.id for row in ordered] == [1, 2, 3, 4, 5, 6]

        last_pinned = ordered[2]
        assert last_pinned.pinned_at is not None
        into_unpinned = await _pinch().after((last_pinned.pinned_at, last_pinned.id)).all()
        assert [row.id for row in into_unpinned] == [4, 5, 6]

        from_null = await _pinch().after((None, 4)).all()
        assert [row.id for row in from_null] == [5, 6]

        position = _pinch().position_of(ordered[3])
        assert position == (None, 4)
        from_row = await _pinch().after(ordered[3]).all()
        assert [row.id for row in from_row] == [5, 6]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_after_non_null_includes_later_null_bucket(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_pinch_convos()
        mid_pinned = await _pinch().after((datetime(2026, 3, 1, tzinfo=UTC), 1)).all()
        assert [row.id for row in mid_pinned] == [2, 3, 4, 5, 6]
