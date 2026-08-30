"""Backend-matrix e2e for ``order_by(..., nulls=...)`` placement (#363, #392).

Asserts result-set order only — same row order on SQLite and Postgres when
``nulls=`` is set. Omitted ``nulls=`` compiles to ``last`` and is
cross-backend-asserted.
"""

from typing import Annotated

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Root nullable sort key + unique id tiebreaker.
# ---------------------------------------------------------------------------


class ObnCard(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    pinned_at: str | None = None
    name: str = ""


async def _seed_cards() -> None:
    """Mixed set + NULL pinned_at values; unique ids for the tiebreaker."""
    for row in (
        ObnCard(id=1, pinned_at="2024-06-01", name="mid"),
        ObnCard(id=2, pinned_at=None, name="null-a"),
        ObnCard(id=3, pinned_at="2024-12-01", name="late"),
        ObnCard(id=4, pinned_at=None, name="null-b"),
        ObnCard(id=5, pinned_at="2024-01-01", name="early"),
    ):
        await row.save()


# ---------------------------------------------------------------------------
# Left-join traversal: related column is NOT NULL on the related model;
# NULLs appear because the relation is missing.
# ---------------------------------------------------------------------------


class ObnBoard(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str = ""
    cards: Relation[list["ObnBoardCard"]] = BackRef()


class ObnBoardCard(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    board: Annotated[ObnBoard | None, ForeignKey(related_name="cards")] = None


async def _seed_board_cards() -> None:
    zeta = ObnBoard(id=1, title="zeta")
    alpha = ObnBoard(id=2, title="alpha")
    await zeta.save()
    await alpha.save()
    await ObnBoardCard(id=1, label="on-zeta", board=zeta).save()
    await ObnBoardCard(id=2, label="on-alpha", board=alpha).save()
    await ObnBoardCard(id=3, label="orphan", board=None).save()


# ---------------------------------------------------------------------------
# Grouped aggregate: SUM of all-NULL amounts is NULL (empty numeric input).
# ---------------------------------------------------------------------------


class ObnItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    category: str = ""
    amount: int | None = None


async def _seed_items() -> None:
    for row in (
        ObnItem(id=1, category="a", amount=100),
        ObnItem(id=2, category="a", amount=50),
        ObnItem(id=3, category="b", amount=None),
        ObnItem(id=4, category="c", amount=10),
        ObnItem(id=5, category="d", amount=None),
        ObnItem(id=6, category="d", amount=None),
    ):
        await row.save()


# ---------------------------------------------------------------------------
# Acceptance.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_omitted_nulls_means_last_on_both_backends(db_url):
    """Omitted nulls= on DESC sorts set values first, NULLs last, on both backends."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_cards()

        rows = await (
            ObnCard.select()
            .order_by(lambda c: c.pinned_at, "desc")
            .order_by(lambda c: c.id)
            .all()
        )

        assert [r.id for r in rows] == [3, 1, 5, 2, 4]


@pytest.mark.asyncio
async def test_desc_nulls_last_same_order_on_both_backends(db_url):
    """DESC + nulls=\"last\": set values lead, NULLs trail; id tiebreaker."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_cards()

        rows = await (
            ObnCard.select()
            .order_by(lambda c: c.pinned_at, "desc", nulls="last")
            .order_by(lambda c: c.id)
            .all()
        )

        assert [r.id for r in rows] == [3, 1, 5, 2, 4]
        assert [r.pinned_at for r in rows] == [
            "2024-12-01",
            "2024-06-01",
            "2024-01-01",
            None,
            None,
        ]


@pytest.mark.asyncio
async def test_desc_nulls_first_same_order_on_both_backends(db_url):
    """nulls=\"first\" on the same seed: NULLs lead; id tiebreaker among NULLs."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_cards()

        rows = await (
            ObnCard.select()
            .order_by(lambda c: c.pinned_at, "desc", nulls="first")
            .order_by(lambda c: c.id)
            .all()
        )

        assert [r.id for r in rows] == [2, 4, 3, 1, 5]


@pytest.mark.asyncio
async def test_chained_terms_first_carries_nulls_later_do_not(db_url):
    """First term carries nulls=; later tiebreakers omit it — order matches SQL."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        # Two rows share pinned_at=None; tiebreak on name DESC then id ASC.
        for row in (
            ObnCard(id=1, pinned_at="2024-06-01", name="mid"),
            ObnCard(id=2, pinned_at=None, name="zebra"),
            ObnCard(id=3, pinned_at=None, name="alpha"),
            ObnCard(id=4, pinned_at="2024-12-01", name="late"),
        ):
            await row.save()

        rows = await (
            ObnCard.select()
            .order_by(lambda c: c.pinned_at, "desc", nulls="last")
            .order_by(lambda c: c.name, "desc")
            .order_by(lambda c: c.id)
            .all()
        )

        # Set values DESC (12-01, 06-01), then NULLs with name DESC (zebra, alpha).
        assert [r.id for r in rows] == [4, 1, 2, 3]


@pytest.mark.asyncio
async def test_left_join_related_not_null_column_honors_nulls(db_url):
    """Traversed forward-FK: board.title is NOT NULL on Board; LEFT join NULLs
    from missing relation honor nulls= placement on both backends."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_board_cards()

        last = await (
            ObnBoardCard.select()
            .left_join(lambda c: c.board)
            .order_by(lambda c: c.board.title, "asc", nulls="last")
            .order_by(lambda c: c.id)
            .all()
        )
        assert [r.id for r in last] == [2, 1, 3]

        first = await (
            ObnBoardCard.select()
            .left_join(lambda c: c.board)
            .order_by(lambda c: c.board.title, "asc", nulls="first")
            .order_by(lambda c: c.id)
            .all()
        )
        assert [r.id for r in first] == [3, 2, 1]


@pytest.mark.asyncio
async def test_projected_aggregate_sum_nulls_last(db_url):
    """order_by(lambda t: t.amount.sum(), \"desc\", nulls=\"last\") — SUM of
    all-NULL groups is NULL and trails the set totals on both backends."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_items()

        rows = await (
            ObnItem.select(
                lambda t: {"category": t.category, "total": t.amount.sum()}
            )
            .order_by(lambda t: t.amount.sum(), "desc", nulls="last")
            .order_by("category")
            .all()
        )

        assert [r.category for r in rows] == ["a", "c", "b", "d"]
        assert [r.total for r in rows] == [150, 10, None, None]
