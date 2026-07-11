"""Backend-matrix integration tests for partial selects (#279).

``select(lambda t: (t.id, t.amount))`` end to end: the record materialization
plan renders a subset SELECT and decodes into ``Row`` records delivered in the
list-like ``Rows`` container, on real SQLite and isolated-schema Postgres —
no SQL-string snapshots at this layer (the rendered SELECT-list pins live in
the Rust walker unit tests). Covers result values/order, container semantics,
typed-column decode parity vs full hydration, build-time selector validation,
verb composition per the PRD #277 verb table, identity-map bypass, and the
validation-free construction pin (I-2).
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest

import ferro
from ferro import FerroField, ForeignKey, Model, Relation, BackRef
from ferro.query import ProjectedQuery, Query, Row, Rows

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: Transaction -> Account, plus a typed-column model for decode parity.
# ---------------------------------------------------------------------------


class PSAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    transactions: Relation[list["PSTransaction"]] = BackRef()


class PSTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    note: str = ""
    account: Annotated[PSAccount, ForeignKey(related_name="transactions")]


class PSTier(StrEnum):
    FREE = "free"
    PRO = "pro"


class PSTyped(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    created_at: datetime
    external_id: UUID
    tier: PSTier = PSTier.FREE
    balance: Decimal | None = None
    name: str = ""


_T1 = datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
_UID1 = UUID("11111111-1111-1111-1111-111111111111")


async def _seed_transactions():
    """One account with three transactions (amounts 10/20/30)."""
    acct = PSAccount(id=1, label="a1")
    await acct.save()
    for i, amount in enumerate((10, 20, 30), start=1):
        await PSTransaction(id=i, amount=amount, note=f"n{i}", account=acct).save()
    return acct


async def _seed_typed():
    row = PSTyped(
        id=1,
        created_at=_T1,
        external_id=_UID1,
        tier=PSTier.PRO,
        balance=Decimal("12.50"),
        name="alice",
    )
    await row.save()
    return row


# ---------------------------------------------------------------------------
# The tracer bullet: values, order, container semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_lambda_returns_rows_of_typed_records(db_url):
    """The projected query returns correct values with field order equal to
    selection order — amount deliberately selected before id."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await PSTransaction.select(lambda t: (t.amount, t.id)).all()

        assert isinstance(rows, Rows)
        assert len(rows) == 3
        assert all(isinstance(row, Row) for row in rows)
        assert {(row.id, row.amount) for row in rows} == {(1, 10), (2, 20), (3, 30)}
        # Field order = selection order (amount first), pinned via model_dump.
        assert list(rows[0].model_dump()) == ["amount", "id"]


@pytest.mark.asyncio
async def test_select_single_field_form(db_url):
    """``select(lambda t: t.amount)`` — one column, no tuple ceremony."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await PSTransaction.select(lambda t: t.amount).order_by("amount").all()

        assert [row.amount for row in rows] == [10, 20, 30]
        assert list(rows[0].model_dump()) == ["amount"]


@pytest.mark.asyncio
async def test_rows_is_list_like_and_pydantic_shaped(db_url):
    """Index, slice, iterate, len; model_dump() yields list[dict]; a slice is
    itself a Rows."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await PSTransaction.select(lambda t: (t.id, t.amount)).order_by("id").all()

        assert len(rows) == 3
        assert rows[0].id == 1
        assert [row.id for row in rows] == [1, 2, 3]
        head = rows[:2]
        assert isinstance(head, Rows) and len(head) == 2
        dumped = rows.model_dump()
        assert dumped == [
            {"id": 1, "amount": 10},
            {"id": 2, "amount": 20},
            {"id": 3, "amount": 30},
        ]
        # JSON serialization works end to end (FastAPI response_model shape).
        assert json.loads(rows.model_dump_json()) == dumped


@pytest.mark.asyncio
async def test_row_is_a_record_not_a_model_instance(db_url):
    """A Row is not a PSTransaction and exposes no persistence surface."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        row = await PSTransaction.select(lambda t: (t.id, t.amount)).first()

        assert row is not None
        assert isinstance(row, Row)
        assert not isinstance(row, Model)
        assert not isinstance(row, PSTransaction)
        assert not hasattr(row, "save")
        assert not hasattr(row, "delete")


# ---------------------------------------------------------------------------
# Typed-column decode parity vs full hydration (both backends).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projected_typed_columns_decode_identically_to_full_hydration(db_url):
    """datetime / uuid / native enum / decimal round-trip through a projection
    with the same types and values as full hydration — a projection is never a
    different dialect of the data."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed()

        full = await PSTyped.select().first()
        row = await PSTyped.select(
            lambda t: (t.created_at, t.external_id, t.tier, t.balance)
        ).first()

        assert full is not None and row is not None
        for field in ("created_at", "external_id", "tier", "balance"):
            hydrated = getattr(full, field)
            projected = getattr(row, field)
            assert type(projected) is type(hydrated), field
            assert projected == hydrated, field
        assert isinstance(row.created_at, datetime)
        assert isinstance(row.external_id, UUID)
        assert isinstance(row.tier, PSTier) and row.tier is PSTier.PRO
        assert isinstance(row.balance, Decimal) and row.balance == Decimal("12.50")


# ---------------------------------------------------------------------------
# Build-time selector validation.
# ---------------------------------------------------------------------------


class TestSelectorValidation:
    def test_misspelled_column_raises_with_did_you_mean(self):
        with pytest.raises(AttributeError, match="Did you mean 'amount'"):
            PSTransaction.select(lambda t: (t.id, t.amonut))

    def test_traversed_column_raises_pointed_not_yet_error(self):
        with pytest.raises(NotImplementedError, match="account.label.*#282"):
            PSTransaction.select(lambda t: t.account.label)

    def test_bare_relation_raises(self):
        with pytest.raises(TypeError, match="bare relation 'account'"):
            PSTransaction.select(lambda t: t.account)

    def test_comparison_raises(self):
        with pytest.raises(TypeError, match="comparison, not a column"):
            PSTransaction.select(lambda t: t.id == 1)

    def test_duplicate_column_raises(self):
        with pytest.raises(ValueError, match="'id' more than once"):
            PSTransaction.select(lambda t: (t.id, t.id))

    def test_empty_selection_raises(self):
        with pytest.raises(ValueError, match="selected no columns"):
            PSTransaction.select(lambda t: ())

    def test_non_callable_selector_raises(self):
        with pytest.raises(TypeError, match="selector callable"):
            PSTransaction.select().select(123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_bare_select_stays_a_full_query(self):
        q = PSTransaction.select()
        assert isinstance(q, Query)
        assert not isinstance(q, ProjectedQuery)

    def test_projection_does_not_mutate_the_source_query(self):
        base = PSTransaction.where(lambda t: t.amount >= 20)
        projected = base.select(lambda t: (t.id,))
        assert isinstance(projected, ProjectedQuery)
        assert not isinstance(base, ProjectedQuery)
        assert not hasattr(base, "_projection")


# ---------------------------------------------------------------------------
# Verb composition (PRD #277 verb table).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_composes_with_where_order_limit_offset(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await (
            PSTransaction.select(lambda t: (t.id, t.amount))
            .where(lambda t: t.amount >= 20)
            .order_by(lambda t: t.amount, "desc")
            .limit(1)
            .offset(1)
            .all()
        )
        assert [(row.id, row.amount) for row in rows] == [(2, 20)]


@pytest.mark.asyncio
async def test_projection_composes_with_relation_traversal_predicate(db_url):
    """where() traversal (one JOIN) narrows a projected query like any other."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        acct = await _seed_transactions()
        other = PSAccount(id=2, label="b1")
        await other.save()
        await PSTransaction(id=9, amount=99, note="other", account=other).save()

        rows = await (
            PSTransaction.select(lambda t: (t.id, t.amount))
            .where(lambda t: t.account.label == "a1")
            .order_by("id")
            .all()
        )
        assert [row.id for row in rows] == [1, 2, 3]
        assert acct.label == "a1"


@pytest.mark.asyncio
async def test_order_by_unselected_column(db_url):
    """order_by may sort by a column the projection does not select."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await PSTransaction.select(lambda t: t.id).order_by("amount", "desc").all()

        assert [row.id for row in rows] == [3, 2, 1]
        assert "amount" not in rows[0].model_dump()


@pytest.mark.asyncio
async def test_first_returns_row_or_none(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        row = await (
            PSTransaction.select(lambda t: (t.id,)).order_by("amount", "desc").first()
        )
        assert isinstance(row, Row) and row.id == 3

        empty = await (
            PSTransaction.select(lambda t: (t.id,))
            .where(lambda t: t.amount > 1000)
            .first()
        )
        assert empty is None


@pytest.mark.asyncio
async def test_count_and_exists_are_unaffected_by_projection(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        projected = PSTransaction.select(lambda t: (t.id,)).where(
            lambda t: t.amount >= 20
        )
        assert await projected.count() == 2
        assert await projected.exists() is True
        assert (
            await PSTransaction.select(lambda t: (t.id,))
            .where(lambda t: t.amount > 1000)
            .exists()
            is False
        )


# ---------------------------------------------------------------------------
# No persistence identity: identity-map bypass, no markers, no validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projected_records_bypass_the_identity_map(db_url):
    """A projection neither reads nor populates the session identity map, and
    a Row carries no Ferro markers (`__dict__` stays empty — projected values
    live in pydantic's extra storage)."""
    from ferro._core import identity_map_len

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session() as s:
        await _seed_transactions()
        instances = await PSTransaction.select().all()
        tracked_before = identity_map_len(s.session_id)
        assert tracked_before >= len(instances)

        rows = await PSTransaction.select(lambda t: (t.id, t.amount)).all()

        assert identity_map_len(s.session_id) == tracked_before
        assert rows[0].__dict__ == {}
        assert "__ferro_persisted" not in rows[0].model_dump()


@pytest.mark.asyncio
async def test_row_construction_never_runs_pydantic_init(db_url, monkeypatch):
    """Structural pin (I-2): the record hot path allocates via __new__ and
    writes slots directly — pydantic __init__/validation would blow up here
    if it were ever called."""

    def _boom(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("pydantic __init__ reached the record hot path")

    monkeypatch.setattr(Row, "__init__", _boom)
    monkeypatch.setattr(Rows, "__init__", _boom)

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_transactions()

        rows = await PSTransaction.select(lambda t: (t.id, t.amount)).order_by("id").all()

        assert len(rows) == 3
        assert rows[0].amount == 10
