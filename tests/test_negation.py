"""End-to-end behavior of uniform predicate negation (`~`, #310).

Prefix `~` negates any predicate node — leaf comparison or AND/OR compound —
and renders as a faithful SQL ``NOT (...)`` over the child. These tests assert
result sets only, on both database backends via the backend matrix; the wire
shape is pinned separately by the ``not`` golden vectors.
"""

import pytest
from typing import Annotated
from ferro import BackRef, Model, ForeignKey, Relation, connect, FerroField, engines

pytestmark = pytest.mark.backend_matrix


class NegProduct(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    price: float
    category: str


async def _seed_products():
    await NegProduct(name="apple", price=10.0, category="fruit").save()
    await NegProduct(name="banana", price=20.0, category="fruit").save()
    await NegProduct(name="carrot", price=30.0, category="veg").save()
    await NegProduct(name="donut", price=40.0, category="bakery").save()


async def _names(query) -> list[str]:
    return sorted(r.name for r in await query.all())


@pytest.mark.asyncio
async def test_not_over_equality_and_inequality(db_url):
    """`~(col == v)` matches the `!=` rows and `~(col != v)` the `==` rows."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        negated_eq = await _names(
            NegProduct.where(lambda p: ~(p.category == "fruit"))
        )
        assert negated_eq == ["carrot", "donut"]
        assert negated_eq == await _names(
            NegProduct.where(lambda p: p.category != "fruit")
        )

        negated_ne = await _names(
            NegProduct.where(lambda p: ~(p.category != "fruit"))
        )
        assert negated_ne == ["apple", "banana"]


@pytest.mark.asyncio
async def test_not_over_ordering_comparisons(db_url):
    """`~` over `<`, `<=`, `>`, `>=` selects exactly the complement rows."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        assert await _names(NegProduct.where(lambda p: ~(p.price < 30))) == [
            "carrot",
            "donut",
        ]
        assert await _names(NegProduct.where(lambda p: ~(p.price <= 30))) == ["donut"]
        assert await _names(NegProduct.where(lambda p: ~(p.price > 30))) == [
            "apple",
            "banana",
            "carrot",
        ]
        assert await _names(NegProduct.where(lambda p: ~(p.price >= 30))) == [
            "apple",
            "banana",
        ]


@pytest.mark.asyncio
async def test_not_in_closes_the_not_in_gap(db_url):
    """`~t.col.in_(values)` is NOT IN — the previously unspellable shape."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        rows = await _names(
            NegProduct.where(lambda p: ~p.category.in_(["fruit", "veg"]))
        )
        assert rows == ["donut"]


@pytest.mark.asyncio
async def test_not_like_closes_the_not_like_gap(db_url):
    """`~t.col.like(pattern)` is NOT LIKE."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        rows = await _names(NegProduct.where(lambda p: ~p.name.like("%a%")))
        assert rows == ["donut"]


@pytest.mark.asyncio
async def test_not_over_compounds(db_url):
    """`~` over an AND/OR compound negates the whole group — no hand-applied
    De Morgan required."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        not_and = await _names(
            NegProduct.where(
                lambda p: ~((p.category == "fruit") & (p.price >= 20))
            )
        )
        assert not_and == ["apple", "carrot", "donut"]

        not_or = await _names(
            NegProduct.where(
                lambda p: ~((p.category == "fruit") | (p.price >= 30))
            )
        )
        assert not_or == []


@pytest.mark.asyncio
async def test_double_negation_round_trips(db_url):
    """`~~p` selects exactly the rows `p` selects."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        plain = await _names(NegProduct.where(lambda p: p.category == "fruit"))
        doubled = await _names(NegProduct.where(lambda p: ~~(p.category == "fruit")))
        assert doubled == plain == ["apple", "banana"]


@pytest.mark.asyncio
async def test_not_mixed_into_and_or_trees(db_url):
    """Negated nodes compose with `&`/`|` at any nesting level."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        rows = await _names(
            NegProduct.where(
                lambda p: (~(p.category == "fruit") & (p.price < 40))
                | ~(p.name.like("%o%"))
            )
        )
        # Left branch: carrot (non-fruit under 40). Right branch: no "o" in
        # the name — apple, banana.
        assert rows == ["apple", "banana", "carrot"]


@pytest.mark.asyncio
async def test_negation_composes_with_order_by_and_limit(db_url):
    """Adding `~` never restructures the query: ordering and paging chain on."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await _seed_products()

        rows = (
            await NegProduct.where(lambda p: ~(p.category == "fruit"))
            .order_by("price", direction="desc")
            .limit(1)
            .all()
        )
        assert [r.name for r in rows] == ["donut"]


class NegReading(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    sensor: str
    amount: float | None = None


@pytest.mark.asyncio
async def test_null_rows_are_excluded_under_negation(db_url):
    """SQL three-valued logic: `~(t.amount > 5)` excludes NULL-amount rows —
    NOT UNKNOWN is UNKNOWN, exactly as the existing `!=` spelling behaves."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await NegReading(sensor="low", amount=3.0).save()
        await NegReading(sensor="high", amount=9.0).save()
        await NegReading(sensor="missing", amount=None).save()

        negated = sorted(
            r.sensor for r in await NegReading.where(lambda r: ~(r.amount > 5)).all()
        )
        assert negated == ["low"]  # NOT a Python set complement: no "missing"

        # Same three-valued behavior as the pre-existing `!=` spelling.
        ne_rows = sorted(
            r.sensor for r in await NegReading.where(lambda r: r.amount != 9.0).all()
        )
        assert ne_rows == ["low"]


class NegAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    transactions: Relation[list["NegTxn"]] = BackRef()


class NegTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: float
    account: Annotated[
        NegAccount | None, ForeignKey(related_name="transactions")
    ] = None


@pytest.mark.asyncio
async def test_not_over_traversed_leaf(db_url):
    """`~` over a relation-traversing leaf keeps the traversal's join."""
    await connect(db_url, auto_migrate=True)
    async with engines.session():
        checking = NegAccount(label="checking")
        await checking.save()
        savings = NegAccount(label="savings")
        await savings.save()
        await NegTxn(amount=5.0, account=checking).save()
        await NegTxn(amount=7.0, account=savings).save()

        rows = await NegTxn.where(lambda t: ~(t.account.label == "checking")).all()
        assert [r.amount for r in rows] == [7.0]
