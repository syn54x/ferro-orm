"""Backend-matrix integration tests for global aggregates (#294, ADR-0009).

The five aggregate methods on scalar field proxies — ``t.amount.sum()``,
traversal included (``t.account.balance.avg()``) — landing in projected
records via the v5 ``expr`` plan, without grouping: an aggregate-only
projection collapses to exactly one record, read idiomatically with
``first()``. Pins the cross-backend decode-type contract per source family
(``count → int``; ``min``/``max`` → source type; ``sum`` → source numeric
type; ``avg`` → float for int/float, Decimal for Decimal), SQL's empty-input
semantics passing through verbatim (``None``/``count=0``, no hidden
COALESCE), and the build-time rejection catalog: disallowed source families,
aggregates inside ``where()`` (→ having(), #291), iterating a field proxy
(the builtin-``sum`` trap), and unaliased aggregates. Mixed aggregate/plain
projections are GROUPED queries — tests/test_grouped_aggregates.py (#295).
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro.query import Row

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: typed columns for the decode contract, a relation for traversal.
# ---------------------------------------------------------------------------


class GATier(StrEnum):
    FREE = "free"
    PRO = "pro"


class GAAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    balance: Decimal | None = None
    transactions: Relation[list["GATransaction"]] = BackRef()


class GATransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    rate: float = 0.0
    price: Decimal | None = None
    note: str | None = None
    happened_at: datetime | None = None
    tier: GATier = GATier.FREE
    flagged: bool = False
    external_id: UUID | None = None
    payload: dict | None = None
    account: Annotated[
        GAAccount | None, ForeignKey(related_name="transactions")
    ] = None


_T1 = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 6, 15, 20, 30, 0, tzinfo=timezone.utc)


async def _seed():
    """Two accounts (balances 100.50 / 200.00); three transactions on a1 and
    one account-less transaction; typed columns populated for the contract."""
    a1 = GAAccount(id=1, name="a1", balance=Decimal("100.50"))
    a2 = GAAccount(id=2, name="a2", balance=Decimal("200.00"))
    await a1.save()
    await a2.save()
    await GATransaction(
        id=1, amount=10, rate=0.5, price=Decimal("1.25"), note="x",
        happened_at=_T1, account=a1,
    ).save()
    await GATransaction(
        id=2, amount=20, rate=1.5, price=Decimal("2.75"), note=None,
        happened_at=_T2, account=a1,
    ).save()
    await GATransaction(
        id=3, amount=30, rate=2.0, price=Decimal("6.00"), note="z",
        account=a1,
    ).save()
    await GATransaction(id=4, amount=40, rate=4.0, account=a2).save()


# ---------------------------------------------------------------------------
# The five aggregates + the pinned decode-type contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_int_source_contract(db_url):
    """sum(int) → int, avg(int) → float, min/max(int) → int, count → int."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await GATransaction.select(
            lambda t: {
                "n": t.id.count(),
                "total": t.amount.sum(),
                "mean": t.amount.avg(),
                "lo": t.amount.min(),
                "hi": t.amount.max(),
            }
        ).first()

        assert row is not None
        assert row.n == 4 and type(row.n) is int
        assert row.total == 100 and type(row.total) is int
        assert row.mean == 25.0 and type(row.mean) is float
        assert row.lo == 10 and type(row.lo) is int
        assert row.hi == 40 and type(row.hi) is int


@pytest.mark.asyncio
async def test_float_source_contract(db_url):
    """sum(float) → float, avg(float) → float, min/max(float) → float."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await GATransaction.select(
            lambda t: {
                "total": t.rate.sum(),
                "mean": t.rate.avg(),
                "lo": t.rate.min(),
                "hi": t.rate.max(),
            }
        ).first()

        assert row is not None
        assert row.total == 8.0 and type(row.total) is float
        assert row.mean == 2.0 and type(row.mean) is float
        assert row.lo == 0.5 and type(row.lo) is float
        assert row.hi == 4.0 and type(row.hi) is float


@pytest.mark.asyncio
async def test_decimal_source_contract(db_url):
    """sum/avg/min/max over Decimal → Decimal — avg deliberately included
    (float would silently lose the source's exactness)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            GATransaction.select(
                lambda t: {
                    "total": t.price.sum(),
                    "mean": t.price.avg(),
                    "lo": t.price.min(),
                    "hi": t.price.max(),
                }
            )
            .where(lambda t: t.price != None)  # noqa: E711
            .first()
        )

        assert row is not None
        assert type(row.total) is Decimal and row.total == Decimal("10.00")
        assert type(row.mean) is Decimal
        assert row.mean == Decimal("10.00") / 3 or abs(
            row.mean - Decimal("3.3333333333333333")
        ) < Decimal("0.000001")
        assert type(row.lo) is Decimal and row.lo == Decimal("1.25")
        assert type(row.hi) is Decimal and row.hi == Decimal("6.00")


@pytest.mark.asyncio
async def test_min_max_over_text_and_datetime(db_url):
    """min/max are ordered, not numeric: text and datetime sources decode to
    the source type via the source codec."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await GATransaction.select(
            lambda t: {
                "first_note": t.note.min(),
                "last_note": t.note.max(),
                "earliest": t.happened_at.min(),
                "latest": t.happened_at.max(),
            }
        ).first()

        assert row is not None
        assert row.first_note == "x" and row.last_note == "z"
        assert isinstance(row.earliest, datetime) and row.earliest == _T1
        assert isinstance(row.latest, datetime) and row.latest == _T2


@pytest.mark.asyncio
async def test_count_skips_nulls_sql_semantics_pass_through(db_url):
    """COUNT(column) counts non-NULL values — SQL semantics verbatim."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await GATransaction.select(
            lambda t: {"rows": t.id.count(), "notes": t.note.count()}
        ).first()

        assert row is not None
        assert row.rows == 4
        assert row.notes == 2  # two of four notes are NULL/absent


# ---------------------------------------------------------------------------
# The global-aggregate shape: one record, first() idiom, empty input.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_only_projection_collapses_to_one_record(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await GATransaction.select(lambda t: {"total": t.amount.sum()}).all()

        assert len(rows) == 1
        assert isinstance(rows[0], Row)
        assert rows[0].total == 100


@pytest.mark.asyncio
async def test_empty_input_none_and_count_zero(db_url):
    """Over zero matching rows the aggregates answer with SQL's own empty
    semantics: one record, sum/avg/min/max None, count 0 — no COALESCE."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            GATransaction.select(
                lambda t: {
                    "n": t.id.count(),
                    "total": t.amount.sum(),
                    "mean": t.amount.avg(),
                    "lo": t.amount.min(),
                    "hi": t.amount.max(),
                }
            )
            .where(lambda t: t.amount > 10_000)
            .first()
        )

        assert row is not None, "a global aggregate always yields one record"
        assert row.n == 0 and type(row.n) is int
        assert row.total is None
        assert row.mean is None
        assert row.lo is None and row.hi is None


@pytest.mark.asyncio
async def test_aggregates_compose_with_where_traversal(db_url):
    """where() traversal narrows the aggregated rows (shared join identity)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            GATransaction.select(lambda t: {"total": t.amount.sum()})
            .where(lambda t: t.account.name == "a1")
            .first()
        )

        assert row is not None and row.total == 60


@pytest.mark.asyncio
async def test_traversed_aggregate_source(db_url):
    """t.account.balance.avg(): the source traverses; INNER narrowing drops
    the account-less row from the aggregation; Decimal contract holds."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()
        await GATransaction(id=9, amount=99).save()  # NULL FK — excluded

        row = await GATransaction.select(
            lambda t: {"n": t.id.count(), "avg_balance": t.account.balance.avg()}
        ).first()

        assert row is not None
        assert row.n == 4, "INNER traversal narrows to rows with an account"
        # a1 counted three times (100.50), a2 once (200.00): 501.50 / 4.
        assert type(row.avg_balance) is Decimal
        assert row.avg_balance == Decimal("125.375")


# ---------------------------------------------------------------------------
# Build-time rejections: source families with no portable meaning.
# ---------------------------------------------------------------------------


class TestSourceFamilyValidation:
    def test_sum_over_text_raises(self):
        with pytest.raises(TypeError, match=r"t\.note\.sum\(\).*string-typed.*numeric"):
            GATransaction.select(lambda t: {"x": t.note.sum()})

    def test_avg_over_datetime_raises(self):
        with pytest.raises(TypeError, match=r"datetime-typed.*numeric"):
            GATransaction.select(lambda t: {"x": t.happened_at.avg()})

    def test_sum_over_bool_raises(self):
        with pytest.raises(TypeError, match="boolean-typed"):
            GATransaction.select(lambda t: {"x": t.flagged.sum()})

    def test_min_over_uuid_raises(self):
        with pytest.raises(TypeError, match="uuid-typed.*orderable"):
            GATransaction.select(lambda t: {"x": t.external_id.min()})

    def test_max_over_enum_raises(self):
        with pytest.raises(TypeError, match="enum-typed"):
            GATransaction.select(lambda t: {"x": t.tier.max()})

    def test_min_over_json_raises(self):
        with pytest.raises(TypeError, match="json-typed"):
            GATransaction.select(lambda t: {"x": t.payload.min()})

    def test_count_takes_any_column(self):
        # count() is the one aggregate every family supports.
        for selector in (
            lambda t: {"x": t.tier.count()},
            lambda t: {"x": t.external_id.count()},
            lambda t: {"x": t.payload.count()},
            lambda t: {"x": t.flagged.count()},
        ):
            assert GATransaction.select(selector) is not None

    def test_traversed_family_validation_names_the_hop_model(self):
        with pytest.raises(TypeError, match=r"GAAccount\.name is string-typed"):
            GATransaction.select(lambda t: {"x": t.account.name.sum()})


# ---------------------------------------------------------------------------
# Build-time rejections: aggregate misuse.
# ---------------------------------------------------------------------------


class TestAggregateMisuse:
    def test_aggregate_comparison_in_where_points_at_having(self):
        with pytest.raises(TypeError, match=r"having\(\) \(#291\)"):
            GATransaction.where(lambda t: t.amount.sum() > 100)

    def test_bare_aggregate_in_where_points_at_having(self):
        with pytest.raises(TypeError, match=r"having\(\) \(#291\)"):
            GATransaction.where(lambda t: t.amount.sum())

    def test_iterating_a_field_proxy_names_the_sum_method(self):
        with pytest.raises(
            TypeError, match=r"did you mean t\.amount\.sum\(\)\?"
        ):
            GATransaction.select(lambda t: {"x": sum(t.amount)})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_unaliased_aggregate_in_tuple_names_the_dict_form(self):
        with pytest.raises(TypeError, match=r"user-named.*dict"):
            GATransaction.select(lambda t: (t.amount.sum(),))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_unaliased_single_aggregate_names_the_dict_form(self):
        with pytest.raises(TypeError, match=r"user-named.*dict"):
            GATransaction.select(lambda t: t.amount.sum())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_aggregate_has_no_truth_value(self):
        with pytest.raises(TypeError, match="no truth value"):
            GATransaction.select(
                lambda t: {"x": t.amount.sum() and t.amount.avg()}  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )
