"""End-to-end behavior of existence tests on reverse relations (#314, ADR-0007).

A reverse (BackRef) relation appears in a predicate in exactly one form — the
existence test ``t.rel.exists()`` — rendered as a correlated EXISTS at every
cardinality (one-to-one BackRefs included), negated with ``~``. These tests
assert result sets only, on both database backends via the backend matrix;
the wire shape is pinned separately by the ``exists`` golden vectors.

The model graph mirrors the #307 workload: a transaction with two one-to-one
BackRefs into a transfer link row (membership via either FK column) plus a
to-many BackRef onto split lines.
"""

import pytest
from typing import Annotated

from ferro import BackRef, FerroField, ForeignKey, Model, Relation, connect, engines

pytestmark = pytest.mark.backend_matrix


class ExTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    transfer_out: "ExTransfer" = BackRef()
    transfer_in: "ExTransfer" = BackRef()
    lines: Relation[list["ExLine"]] = BackRef()


class ExTransfer(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    outflow_transaction: Annotated[
        ExTxn | None, ForeignKey(related_name="transfer_out", unique=True)
    ] = None
    inflow_transaction: Annotated[
        ExTxn | None, ForeignKey(related_name="transfer_in", unique=True)
    ] = None


class ExLine(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    txn: Annotated[ExTxn, ForeignKey(related_name="lines", on_delete="CASCADE")]
    category: str = ""


async def _seed_transfers() -> dict[str, ExTxn]:
    """Four transactions: out is the outflow of a transfer, in the inflow of
    another (untracked counterparties — one FK set per transfer), and two
    plain transactions with no transfer membership at all."""
    out = await ExTxn.create(amount=-100)
    inn = await ExTxn.create(amount=100)
    plain_a = await ExTxn.create(amount=-20)
    plain_b = await ExTxn.create(amount=-30)
    await ExTransfer.create(outflow_transaction=out)
    await ExTransfer.create(inflow_transaction=inn)
    return {"out": out, "in": inn, "plain_a": plain_a, "plain_b": plain_b}


async def _amounts(query) -> list[int]:
    return sorted(r.amount for r in await query.all())


@pytest.mark.asyncio
async def test_bare_exists_on_one_to_one_backref(db_url):
    """``t.transfer_out.exists()`` matches exactly the rows with a child row —
    a one-to-one BackRef spells identically to a to-many one."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_transfers()

        rows = await _amounts(ExTxn.where(lambda t: t.transfer_out.exists()))
        assert rows == [-100]


@pytest.mark.asyncio
async def test_is_transfer_true_branch(db_url):
    """The #307 ``is_transfer=true`` filter: membership via EITHER FK column,
    each matching root exactly once."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_transfers()

        rows = await _amounts(
            ExTxn.where(lambda t: t.transfer_out.exists() | t.transfer_in.exists())
        )
        assert rows == [-100, 100]


@pytest.mark.asyncio
async def test_is_transfer_false_branch(db_url):
    """The #307 ``is_transfer=false`` filter: ``~`` renders NOT EXISTS and the
    conjunction keeps only transfer-free transactions."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_transfers()

        rows = await _amounts(
            ExTxn.where(lambda t: ~t.transfer_out.exists() & ~t.transfer_in.exists())
        )
        assert rows == [-30, -20]


@pytest.mark.asyncio
async def test_to_many_exists_returns_each_root_once(db_url):
    """A transaction with three lines matches ``t.lines.exists()`` exactly
    once — the result stays root-shaped (no DISTINCT bookkeeping)."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        split = await ExTxn.create(amount=-70)
        for _ in range(3):
            await ExLine.create(txn=split, category="groceries")
        await ExTxn.create(amount=-10)

        results = await ExTxn.where(lambda t: t.lines.exists()).all()
        assert [r.amount for r in results] == [-70]


@pytest.mark.asyncio
async def test_exists_composes_with_root_predicates_order_and_limit(db_url):
    """An existence test is an ordinary predicate: it AND-composes with root
    comparisons and leaves keyset ``order_by`` + ``limit`` untouched."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        for amount in (-40, -50, -60):
            txn = await ExTxn.create(amount=amount)
            await ExLine.create(txn=txn, category="travel")
        await ExTxn.create(amount=-80)  # child-less

        results = (
            await ExTxn.where(lambda t: t.lines.exists() & (t.amount <= -50))
            .order_by("amount", "desc")
            .limit(1)
            .all()
        )
        assert [r.amount for r in results] == [-50]


@pytest.mark.asyncio
async def test_count_with_exists(db_url):
    """``count()`` sees the same membership as ``all()``."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_transfers()

        n = await ExTxn.where(
            lambda t: t.transfer_out.exists() | t.transfer_in.exists()
        ).count()
        assert n == 2


# ---------------------------------------------------------------------------
# Error surfaces (#314): the reverse proxy exposes .exists() and nothing else.
# Reverse relations are tested, not traversed (ADR-0007); every dead end names
# the supported spelling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reverse_column_access_raises_naming_exists(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(AttributeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.lines.category == "groceries")


@pytest.mark.asyncio
async def test_reverse_none_comparison_raises_naming_exists(db_url):
    """The #307 repro's first guess, ``t.transfer_out != None``, teaches the
    supported spelling instead of failing opaquely."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.transfer_out != None)  # noqa: E711
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.transfer_out == None)  # noqa: E711


@pytest.mark.asyncio
async def test_reverse_ordering_comparisons_raise_naming_exists(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.lines < 5)


@pytest.mark.asyncio
async def test_reverse_in_raises_naming_exists(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.lines.in_([1, 2]))
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.lines << [1, 2])


@pytest.mark.asyncio
async def test_bare_reverse_proxy_in_where_raises_naming_exists(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExTxn.where(lambda t: t.lines)


@pytest.mark.asyncio
async def test_include_rejection_survives_reverse_proxy_resolution(db_url):
    """The pinned include() population rejection (#287) is unchanged now that
    predicate proxies resolve reverse relations: population and membership
    stay distinct axes (ADR-0007)."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="reverse .BackRef. or many-to-many"):
        ExTxn.select().include(lambda t: t.lines)


@pytest.mark.asyncio
async def test_exists_rejected_on_mutating_verbs(db_url):
    """UPDATE/DELETE stay single-table write shapes: an existence test in the
    predicate is rejected at build time, before any DB round-trip."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        with pytest.raises(ValueError, match="update"):
            await ExTxn.where(lambda t: t.lines.exists()).update(amount=0)
        with pytest.raises(ValueError, match="delete"):
            await ExTxn.where(lambda t: t.lines.exists()).delete()
