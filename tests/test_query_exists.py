"""End-to-end behavior of existence tests on reverse relations (ADR-0007).

A reverse (BackRef) relation appears in a predicate in exactly one form — the
existence test ``t.rel.exists(...)`` — rendered as a correlated EXISTS at
every cardinality (one-to-one BackRefs included), negated with ``~``, and
optionally scoped by a full ferro predicate over the related model (#315).
These tests assert result sets only, on both database backends via the
backend matrix; the wire shape is pinned separately by the ``exists`` golden
vectors.

The model graph mirrors the #307/#308 workloads: a transaction with two
one-to-one BackRefs into a transfer link row (membership via either FK
column), a to-many BackRef onto split lines, and a category the lines and
transactions both point at (the line-aware category filter).
"""

import pytest
from typing import Annotated

from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    connect,
    engines,
)

pytestmark = pytest.mark.backend_matrix


class ExCat(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    txns: Relation[list["ExTxn"]] = BackRef()
    lines: Relation[list["ExLine"]] = BackRef()


class ExTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    category: Annotated[
        ExCat | None, ForeignKey(related_name="txns", on_delete="SET NULL")
    ] = None
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
    category: Annotated[
        ExCat | None, ForeignKey(related_name="lines", on_delete="SET NULL")
    ] = None
    amount: int = 0
    memo: str = ""


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
            await ExLine.create(txn=split)
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
            await ExLine.create(txn=txn)
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


# ---------------------------------------------------------------------------
# Scoped inner predicates (#315): the optional inner lambda is a full ferro
# predicate over the child model — every operator, forward traversal (joins
# INSIDE the subquery, ADR-0006 unchanged), nesting — with cross-scope
# references rejected at build time (deferred to #309).
# ---------------------------------------------------------------------------


async def _seed_categories() -> dict[str, object]:
    """The #308 fixture: a split transaction whose three lines carry the
    category (its own category vacated), a plain transaction categorized at
    the root, and a transaction matching neither."""
    groceries = await ExCat.create(name="Groceries")
    travel = await ExCat.create(name="Travel")
    split = await ExTxn.create(amount=-7000)  # category vacated while split
    for amount in (-3000, -2000, -2000):
        await ExLine.create(txn=split, category=groceries, amount=amount)
    plain = await ExTxn.create(amount=-1000, category=groceries)
    other = await ExTxn.create(amount=-500, category=travel)
    return {
        "groceries": groceries,
        "travel": travel,
        "split": split,
        "plain": plain,
        "other": other,
    }


@pytest.mark.asyncio
async def test_scoped_exists_filters_by_child_predicate(db_url):
    """``t.lines.exists(lambda line: ...)`` keeps exactly the roots with a
    matching child row."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        seeded = await _seed_categories()

        rows = await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(
                    lambda line, cat=seeded["groceries"]: line.category_id == cat.id
                )
            )
        )
        assert rows == [-7000]


@pytest.mark.asyncio
async def test_308_line_aware_category_filter(db_url):
    """The full #308 demo: root-or-line category membership. A three-line
    split matches exactly once, the child-less root survives through the OR's
    root branch, and keyset ``order_by`` + ``limit`` compose unchanged."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        seeded = await _seed_categories()
        ids = [seeded["groceries"].id]

        query = ExTxn.where(
            lambda t: (
                t.category_id.in_(ids)
                | t.lines.exists(lambda line: line.category_id.in_(ids))
            )
        )
        rows = await _amounts(query)
        assert rows == [-7000, -1000]

        paged = (
            await ExTxn.where(
                lambda t: (
                    t.category_id.in_(ids)
                    | t.lines.exists(lambda line: line.category_id.in_(ids))
                )
            )
            .order_by("amount", "desc")
            .limit(1)
            .all()
        )
        assert [r.amount for r in paged] == [-1000]


@pytest.mark.asyncio
async def test_inner_lambda_supports_full_operator_set(db_url):
    """Every operator and ``&``/``|``/``~`` composition works over the child
    model — the inner predicate is ordinary ferro, not a sub-language."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        txn = await ExTxn.create(amount=-70)
        await ExLine.create(txn=txn, amount=-30, memo="grocery run")
        await ExLine.create(txn=txn, amount=-40, memo="fuel")
        bare = await ExTxn.create(amount=-10)
        await ExLine.create(txn=bare, amount=5, memo="refund")

        assert await _amounts(
            ExTxn.where(lambda t: t.lines.exists(lambda line: line.memo.like("%fuel%")))
        ) == [-70]
        assert await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(
                    lambda line: (line.amount <= -30) & ~line.memo.like("%grocery%")
                )
            )
        ) == [-70]
        assert await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(
                    lambda line: (line.amount > 0) | (line.amount < -35)
                )
            )
        ) == [-70, -10]


@pytest.mark.asyncio
async def test_forward_traversal_inside_subquery(db_url):
    """A forward-FK traversal inside the inner lambda renders its join INSIDE
    the EXISTS subquery under unchanged ADR-0006 semantics (INNER,
    narrowing)."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_categories()

        rows = await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(lambda line: line.category.name == "Groceries")
            )
        )
        assert rows == [-7000]

        # INNER semantics: a line with no category can never match a
        # traversed inner predicate.
        uncategorized = await ExTxn.create(amount=-42)
        await ExLine.create(txn=uncategorized, amount=-42)
        rows = await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(lambda line: line.category.name != "nope")
            )
        )
        assert rows == [-7000]


@pytest.mark.asyncio
async def test_nested_exists_depth_two(db_url):
    """Existence tests nest: categories with a transaction that has a
    negative line — the exists node's inner tree is an ordinary condition
    tree, so recursion is free."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        cat = await ExCat.create(name="Active")
        idle = await ExCat.create(name="Idle")
        txn = await ExTxn.create(amount=-100, category=cat)
        await ExLine.create(txn=txn, amount=-60)
        pos = await ExTxn.create(amount=200, category=idle)
        await ExLine.create(txn=pos, amount=200)

        results = await ExCat.where(
            lambda c: c.txns.exists(
                lambda t: t.lines.exists(lambda line: line.amount < 0)
            )
        ).all()
        assert [r.name for r in results] == ["Active"]


@pytest.mark.asyncio
async def test_explicit_grouping_contrast(db_url):
    """``exists(lambda line: A & B)`` (one child row matches both) and
    ``exists(A-test) & exists(B-test)`` (some child row matches each) are
    different, correct row sets — the ambiguity ADR-0007 rejects is
    unspellable by construction."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        both_in_one = await ExTxn.create(amount=-10)
        await ExLine.create(txn=both_in_one, amount=-50, memo="fuel")
        spread = await ExTxn.create(amount=-20)
        await ExLine.create(txn=spread, amount=-50, memo="snacks")
        await ExLine.create(txn=spread, amount=-5, memo="fuel")

        one_row_matches_both = await _amounts(
            ExTxn.where(
                lambda t: t.lines.exists(
                    lambda line: (line.amount <= -50) & line.memo.like("%fuel%")
                )
            )
        )
        assert one_row_matches_both == [-10]

        some_row_matches_each = await _amounts(
            ExTxn.where(
                lambda t: (
                    t.lines.exists(lambda line: line.amount <= -50)
                    & t.lines.exists(lambda line: line.memo.like("%fuel%"))
                )
            )
        )
        assert some_row_matches_each == [-20, -10]


@pytest.mark.asyncio
async def test_negated_scoped_exists(db_url):
    """``~t.lines.exists(lambda line: ...)`` renders NOT EXISTS over the scoped
    subquery."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        seeded = await _seed_categories()
        ids = [seeded["groceries"].id]

        rows = await _amounts(
            ExTxn.where(
                lambda t: ~t.lines.exists(lambda line: line.category_id.in_(ids))
            )
        )
        assert rows == [-1000, -500]


@pytest.mark.asyncio
async def test_scoped_exists_count(db_url):
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        seeded = await _seed_categories()
        ids = [seeded["groceries"].id]

        n = await ExTxn.where(
            lambda t: (
                t.category_id.in_(ids)
                | t.lines.exists(lambda line: line.category_id.in_(ids))
            )
        ).count()
        assert n == 2


# ---------------------------------------------------------------------------
# Cross-scope guard (#315): the inner lambda may reference only its own
# parameter's scope; everything else fails at build time pointing at the
# deferred capability (#309). Silent misrendering is the failure mode this
# guard exists to prevent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_scope_outer_column_rejected(db_url):
    """An inner-tree leaf built from the OUTER lambda's parameter is a
    build-time error, not a silently re-scoped column."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="#309"):
        ExTxn.where(lambda t: t.lines.exists(lambda line: t.amount > 5))


@pytest.mark.asyncio
async def test_cross_scope_field_proxy_rhs_rejected(db_url):
    """A FieldProxy as a comparison right-hand side (column-to-column) is a
    build-time error pointing at #309 — whichever scope it came from."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="#309"):
        ExTxn.where(
            lambda t: t.lines.exists(lambda line: line.category_id == t.category_id)
        )
    with pytest.raises(TypeError, match="#309"):
        ExTxn.where(lambda t: t.lines.exists(lambda line: line.amount == line.amount))


@pytest.mark.asyncio
async def test_cross_scope_nested_exists_rejected(db_url):
    """A nested existence test built from the OUTER proxy inside the inner
    lambda is cross-scope too."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="#309"):
        ExTxn.where(lambda t: t.lines.exists(lambda line: t.transfer_out.exists()))


@pytest.mark.asyncio
async def test_inner_lambda_must_return_a_predicate(db_url):
    """A non-predicate inner lambda fails with the same pointed shape as
    where() itself."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="predicate"):
        ExTxn.where(lambda t: t.lines.exists(lambda line: line.amount))


# ---------------------------------------------------------------------------
# Many-to-many (#316): the same verb, the same node, a two-hop correlation
# path — join table first (correlated to the enclosing scope), then the
# target. M2M is test surface, not a second mechanism.
# ---------------------------------------------------------------------------


class ExTag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    users: Relation[list["ExUser"]] = ManyToMany(related_name="tags")


class ExUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    username: str = ""
    tags: Relation[list["ExTag"]] = BackRef()


async def _seed_tags() -> dict[str, object]:
    admin = await ExTag.create(name="admin")
    beta = await ExTag.create(name="beta")
    alice = await ExUser.create(username="alice")
    bob = await ExUser.create(username="bob")
    await ExUser.create(username="carol")  # tag-less
    await admin.users.add(alice)
    await beta.users.add(alice)
    await beta.users.add(bob)
    return {"admin": admin, "beta": beta, "alice": alice, "bob": bob}


async def _usernames(query) -> list[str]:
    return sorted(r.username for r in await query.all())


@pytest.mark.asyncio
async def test_m2m_bare_exists(db_url):
    """Bare ``.exists()`` on an M2M relation: any linked row, each root
    exactly once no matter how many join-table rows match."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()

        rows = await _usernames(ExUser.where(lambda u: u.tags.exists()))
        assert rows == ["alice", "bob"]


@pytest.mark.asyncio
async def test_m2m_scoped_exists(db_url):
    """The #316 demo: ``u.tags.exists(lambda tag: tag.name == "admin")``."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()

        rows = await _usernames(
            ExUser.where(lambda u: u.tags.exists(lambda tag: tag.name == "admin"))
        )
        assert rows == ["alice"]


@pytest.mark.asyncio
async def test_m2m_negated_exists(db_url):
    """``~u.tags.exists(...)`` renders NOT EXISTS over the two-hop path."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()

        assert await _usernames(ExUser.where(lambda u: ~u.tags.exists())) == ["carol"]
        assert await _usernames(
            ExUser.where(lambda u: ~u.tags.exists(lambda tag: tag.name == "admin"))
        ) == ["bob", "carol"]


@pytest.mark.asyncio
async def test_m2m_exists_from_the_declaring_side(db_url):
    """The declaring side spells identically: tags with at least one user."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()
        await ExTag.create(name="unused")

        results = await ExTag.where(lambda t: t.users.exists()).all()
        assert sorted(r.name for r in results) == ["admin", "beta"]


@pytest.mark.asyncio
async def test_m2m_inner_lambda_full_predicate_power(db_url):
    """Operators, combinators, and ``~`` inside the M2M inner lambda, exactly
    like the reverse-FK slice."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()

        rows = await _usernames(
            ExUser.where(
                lambda u: u.tags.exists(
                    lambda tag: (
                        tag.name.in_(["admin", "beta"]) & ~tag.name.like("%adm%")
                    )
                )
            )
        )
        assert rows == ["alice", "bob"]


@pytest.mark.asyncio
async def test_m2m_exists_composes_with_root_predicates(db_url):
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_tags()

        rows = await _usernames(
            ExUser.where(
                lambda u: (
                    u.tags.exists(lambda tag: tag.name == "beta")
                    & (u.username != "bob")
                )
            )
        )
        assert rows == ["alice"]

        n = await ExUser.where(lambda u: u.tags.exists()).count()
        assert n == 2


# ---------------------------------------------------------------------------
# Remaining error surfaces (#317): every place a reverse or M2M relation can
# be named now answers with the supported spelling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_left_join_on_reverse_edge_names_exists(db_url):
    """``left_join()`` on a reverse edge stays rejected — the pinned "a join
    never multiplies root rows" property is preserved by rejection — and the
    error now names ``.exists()``."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\("):
        ExTxn.select().left_join(lambda t: t.transfer_out)
    with pytest.raises(TypeError, match=r"\.exists\("):
        ExTxn.select().join(lambda t: t.lines)


@pytest.mark.asyncio
async def test_left_join_on_m2m_edge_names_exists(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match=r"\.exists\("):
        ExUser.select().left_join(lambda u: u.tags)


@pytest.mark.asyncio
async def test_in_with_query_rhs_names_exists(db_url):
    """``in_()`` with a query RHS stays a TypeError, and the message names the
    existence test when the RHS is a query (the #307 repro's second guess)."""
    await connect(db_url, auto_migrate=True)
    sub = ExLine.select(lambda line: line.txn_id)
    with pytest.raises(TypeError, match=r"\.exists\("):
        ExTxn.where(lambda t: t.id.in_(sub))
    with pytest.raises(TypeError, match=r"\.exists\("):
        ExTxn.where(lambda t: t.id.in_(ExLine.select()))
    # A non-query, non-collection RHS keeps the plain message — no exists
    # hint where none applies.
    with pytest.raises(TypeError, match="expects a list, tuple, or set") as exc_info:
        ExTxn.where(lambda t: t.id.in_(42))
    assert ".exists(" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_include_on_m2m_edge_unchanged(db_url):
    """``include()`` population rejection is unchanged for M2M too — reverse
    population stays a separate future mechanism."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(TypeError, match="reverse .BackRef. or many-to-many"):
        ExUser.select().include(lambda u: u.tags)


@pytest.mark.asyncio
async def test_m2m_proxy_rejects_everything_but_exists(db_url):
    """The M2M reverse proxy has the same single verb as the reverse-FK one."""
    await connect(db_url, auto_migrate=True)
    with pytest.raises(AttributeError, match=r"\.exists\(\)"):
        ExUser.where(lambda u: u.tags.name == "admin")
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExUser.where(lambda u: u.tags != None)  # noqa: E711
    with pytest.raises(TypeError, match=r"\.exists\(\)"):
        ExUser.where(lambda u: u.tags.in_([1]))
