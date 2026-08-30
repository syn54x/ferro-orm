"""Position paging on traversed order keys and projected records (#396, ADR-0018).

Build-time tests need no database. E2e tests run on the backend matrix.
A decoded tuple is enough to page — ``include()`` is not required.
"""

from typing import Annotated

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro.query.rows import Row


class PosTravAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    transactions: Relation[list["PosTravTransaction"]] = BackRef()
    notes: Relation[list["PosTravNote"]] = BackRef()


class PosTravTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[PosTravAccount, ForeignKey(related_name="transactions")]


class PosTravNote(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    body: str = ""
    account: Annotated[PosTravAccount | None, ForeignKey(related_name="notes")] = None


class PosTravAggItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    category: str
    amount: int = 0


def _traversed(model: type[PosTravTransaction] = PosTravTransaction):
    return (
        model.select()
        .order_by(lambda txn: txn.account.label)
        .order_by(lambda txn: txn.id)
    )


# ---------------------------------------------------------------------------
# Build-time (no DB).
# ---------------------------------------------------------------------------


def test_after_accepts_traversed_order_key_from_a_tuple():
    query = _traversed().after(("a1", 1))
    assert query._after == ("a1", 1)


def test_before_accepts_traversed_order_key_from_a_tuple():
    query = _traversed().before(("a1", 1))
    assert query._before == ("a1", 1)


def test_related_id_is_not_the_model_primary_key():
    with pytest.raises(ValueError, match=r"primary key"):
        (PosTravTransaction.select().order_by(lambda txn: txn.account.id).after((1,)))


def test_position_of_unpopulated_traversed_key_raises():
    row = PosTravTransaction.model_construct(id=1, amount=10, account_id=1)
    with pytest.raises(ValueError, match=r"account\.label"):
        _traversed().position_of(row)


def test_position_of_populated_traversed_key_reads_the_related_column():
    account = PosTravAccount.model_construct(id=7, label="a1")
    row = PosTravTransaction.model_construct(id=1, amount=10, account_id=7)
    # include() population: relation name in __dict__ shadows ForwardDescriptor.
    row.__dict__["account"] = account
    assert _traversed().position_of(row) == ("a1", 1)


def test_position_of_populated_none_related_slot_is_legal():
    row = PosTravNote.model_construct(id=3, body="orphan")
    row.__dict__["account"] = None
    position = (
        PosTravNote.select()
        .order_by(lambda note: note.account.label)
        .order_by(lambda note: note.id)
        .position_of(row)
    )
    assert position == (None, 3)


def test_after_none_in_left_joined_related_slot_is_legal():
    query = (
        PosTravNote.select()
        .left_join(lambda note: note.account)
        .order_by(lambda note: note.account.label)
        .order_by(lambda note: note.id)
        .after((None, 3))
    )
    assert query._after == (None, 3)


def test_projected_after_accepts_a_tuple():
    query = (
        PosTravTransaction.select(
            lambda txn: {"label": txn.account.label, "id": txn.id}
        )
        .order_by(lambda txn: txn.account.label)
        .order_by(lambda txn: txn.id)
        .after(("a1", 1))
    )
    assert query._after == ("a1", 1)


def test_projected_position_of_missing_order_key_raises():
    query = (
        PosTravTransaction.select(lambda txn: (txn.id, txn.amount))
        .order_by(lambda txn: txn.account.label)
        .order_by(lambda txn: txn.id)
    )
    row = Row.model_construct(id=1, amount=10)
    with pytest.raises(ValueError, match=r"tuple"):
        query.position_of(row)


def test_projected_position_of_reads_selected_order_keys():
    query = (
        PosTravTransaction.select(
            lambda txn: {"label": txn.account.label, "id": txn.id}
        )
        .order_by(lambda txn: txn.account.label)
        .order_by(lambda txn: txn.id)
    )
    row = Row.model_construct(label="a1", id=1)
    assert query.position_of(row) == ("a1", 1)


def test_grouped_aggregate_after_raises_for_missing_pk():
    query = (
        PosTravAggItem.select(
            lambda item: {"cat": item.category, "total": item.amount.sum()}
        )
        .order_by("total", "desc")
        .order_by("cat")
    )
    with pytest.raises(ValueError, match=r"primary key"):
        query.after((100, "food"))


def test_grouped_aggregate_before_raises_for_missing_pk():
    query = (
        PosTravAggItem.select(
            lambda item: {"cat": item.category, "total": item.amount.sum()}
        )
        .order_by("total", "desc")
        .order_by("cat")
    )
    with pytest.raises(ValueError, match=r"primary key"):
        query.before((100, "food"))


def test_aggregate_order_key_raises_even_when_pk_is_a_group_key():
    query = (
        PosTravAggItem.select(lambda item: {"id": item.id, "total": item.amount.sum()})
        .order_by("total")
        .order_by("id")
    )
    with pytest.raises(ValueError, match=r"aggregate"):
        query.after((100, 1))


def test_expression_order_key_remains_a_build_time_error():
    with pytest.raises(TypeError, match=r"value expressions|FieldProxy"):
        PosTravTransaction.select().order_by(lambda txn: txn.amount + 1)


# ---------------------------------------------------------------------------
# E2e: traversed after/before from a tuple, no include().
# ---------------------------------------------------------------------------


async def _seed_transactions() -> None:
    a1 = await PosTravAccount.create(id=1, label="a1")
    a2 = await PosTravAccount.create(id=2, label="b1")
    await PosTravTransaction.create(id=1, amount=10, account=a1)
    await PosTravTransaction.create(id=2, amount=20, account=a1)
    await PosTravTransaction.create(id=3, amount=30, account=a2)
    await PosTravTransaction.create(id=4, amount=40, account=a2)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_after_traversed_tuple_pages_without_include(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()
        ordered = await _traversed().all()
        assert [row.id for row in ordered] == [1, 2, 3, 4]

        page = await _traversed().after(("a1", 2)).limit(2).all()
        assert [row.id for row in page] == [3, 4]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_before_traversed_tuple_pages_without_include(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()
        page = await _traversed().before(("b1", 3)).limit(2).all()
        assert [row.id for row in page] == [1, 2]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_position_of_instance_requires_populated_path(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()
        bare = await PosTravTransaction.select().where(lambda txn: txn.id == 2).first()
        assert bare is not None
        with pytest.raises(ValueError, match=r"account\.label"):
            _traversed().position_of(bare)

        populated = await (
            PosTravTransaction.select()
            .include(lambda txn: txn.account)
            .where(lambda txn: txn.id == 2)
            .first()
        )
        assert populated is not None
        assert _traversed().position_of(populated) == ("a1", 2)
        page = await _traversed().after(populated).limit(2).all()
        assert [row.id for row in page] == [3, 4]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_left_join_none_related_slot_pages(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        a1 = await PosTravAccount.create(id=1, label="a1")
        await PosTravNote.create(id=1, body="on-a1", account=a1)
        await PosTravNote.create(id=2, body="orphan")

        query = (
            PosTravNote.select()
            .left_join(lambda note: note.account)
            .order_by(lambda note: note.account.label)
            .order_by(lambda note: note.id)
        )
        ordered = await query.all()
        assert [row.id for row in ordered] == [1, 2]

        after_related = await query.after(("a1", 1)).all()
        assert [row.id for row in after_related] == [2]

        after_null = await query.after((None, 2)).all()
        assert after_null == []


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_projected_record_after_before_and_position_of(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()
        query = (
            PosTravTransaction.select(
                lambda txn: {"label": txn.account.label, "id": txn.id}
            )
            .order_by(lambda txn: txn.account.label)
            .order_by(lambda txn: txn.id)
        )
        rows = await query.all()
        assert [row.id for row in rows] == [1, 2, 3, 4]

        position = query.position_of(rows[1])
        assert position == ("a1", 2)

        after_page = await query.after(position).limit(2).all()
        assert [row.id for row in after_page] == [3, 4]

        from_row = await query.after(rows[1]).limit(2).all()
        assert [row.id for row in from_row] == [3, 4]

        before_page = await query.before(("b1", 3)).limit(2).all()
        assert [row.id for row in before_page] == [1, 2]
