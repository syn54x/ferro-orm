"""Backend-matrix integration tests for grouped aggregates (#295, ADR-0009).

Mixed projections are GROUPED queries: every non-aggregate field is a group
key, and GROUP BY is derived from the projection — never declared, never on
the wire (the rendered-SQL pins live in the Rust walker unit tests). Covers
derived grouping with ``where()`` composition (traversal included),
traversed and ``None``-keyed group keys, the top-N idiom, the three
``order_by`` rules (output names first; lambda spells source expressions
including aggregates; on an aggregate projection every sort key is a group
key or an aggregate), ``count()``/``exists()`` guidance errors,
``limit()``/``offset()`` group semantics, and zero-rows → zero records.
"""

from decimal import Decimal
from typing import Annotated

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: Transaction -> Account -> Owner (depth 2), for traversed keys.
# ---------------------------------------------------------------------------


class GRPOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: str = ""
    accounts: Relation[list["GRPAccount"]] = BackRef()


class GRPAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    owner: Annotated[GRPOwner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["GRPTransaction"]] = BackRef()


class GRPTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    price: Decimal | None = None
    note: str = ""
    account: Annotated[
        GRPAccount | None, ForeignKey(related_name="transactions")
    ] = None


async def _seed():
    """Owner alice <- a1 (txns 10, 20), owner bob <- a2 (txn 40), plus an
    account-less transaction (30) for the None-bucket tests."""
    alice = GRPOwner(id=1, email="alice@x.io")
    bob = GRPOwner(id=2, email="bob@x.io")
    await alice.save()
    await bob.save()
    a1 = GRPAccount(id=1, name="a1", owner=alice)
    a2 = GRPAccount(id=2, name="a2", owner=bob)
    await a1.save()
    await a2.save()
    await GRPTransaction(
        id=1, amount=10, price=Decimal("1.00"), note="x", account=a1
    ).save()
    await GRPTransaction(
        id=2, amount=20, price=Decimal("3.00"), note="y", account=a1
    ).save()
    await GRPTransaction(id=3, amount=30, note="orphan").save()
    await GRPTransaction(
        id=4, amount=40, price=Decimal("8.00"), note="z", account=a2
    ).save()


# ---------------------------------------------------------------------------
# Derived grouping: bare fields are the keys.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_projection_groups_by_the_plain_fields(db_url):
    """The canonical grouped query (ADR-0009): one dict lambda, GROUP BY
    derived from the non-aggregate field."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {
                    "acct": t.account_id,
                    "total": t.amount.sum(),
                    "n": t.id.count(),
                }
            )
            .where(lambda t: t.account_id != None)  # noqa: E711
            .order_by("acct")
            .all()
        )

        assert rows.model_dump() == [
            {"acct": 1, "total": 30, "n": 2},
            {"acct": 2, "total": 40, "n": 1},
        ]


@pytest.mark.asyncio
async def test_grouped_query_composes_with_where_traversal(db_url):
    """where() traversal narrows the grouped rows through the shared join."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.account.owner.email == "alice@x.io")
            .all()
        )

        assert rows.model_dump() == [{"acct": 1, "total": 30}]


@pytest.mark.asyncio
async def test_traversed_group_key(db_url):
    """Group keys may traverse (t.account.name), sharing join identity with
    the aggregate's rows."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"account_name": t.account.name, "total": t.amount.sum()}
            )
            .order_by("account_name")
            .all()
        )

        # INNER traversal narrows: the account-less txn (30) is dropped.
        assert rows.model_dump() == [
            {"account_name": "a1", "total": 30},
            {"account_name": "a2", "total": 40},
        ]


@pytest.mark.asyncio
async def test_left_joined_traversed_key_yields_a_none_keyed_group(db_url):
    """'Has no relation' is a visible bucket, not a dropped row: the
    left-joined traversed key groups the orphan under None."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"account_name": t.account.name, "total": t.amount.sum()}
            )
            .left_join(lambda t: t.account)
            .all()
        )

        by_key = {row.account_name: row.total for row in rows}
        assert by_key == {"a1": 30, "a2": 40, None: 30}


@pytest.mark.asyncio
async def test_multiple_group_keys(db_url):
    """Every plain field is a key: two keys group by the pair."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {
                    "acct": t.account_id,
                    "note": t.note,
                    "n": t.id.count(),
                }
            )
            .where(lambda t: t.account_id == 1)
            .order_by("note")
            .all()
        )

        assert rows.model_dump() == [
            {"acct": 1, "note": "x", "n": 1},
            {"acct": 1, "note": "y", "n": 1},
        ]


@pytest.mark.asyncio
async def test_grouped_query_over_zero_rows_returns_zero_records(db_url):
    """Unlike a global aggregate (one record), a grouped query over no rows
    yields no groups at all."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.amount > 10_000)
            .all()
        )

        assert len(rows) == 0


# ---------------------------------------------------------------------------
# The top-N idiom.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_n_idiom_end_to_end(db_url):
    """Keys + aggregates, order_by("total", "desc"), limit — the query the
    epic exists for."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"account_name": t.account.name, "total": t.amount.sum()}
            )
            .order_by("total", "desc")
            .limit(1)
            .all()
        )

        assert rows.model_dump() == [{"account_name": "a2", "total": 40}]


@pytest.mark.asyncio
async def test_limit_and_offset_apply_to_groups(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        base = GRPTransaction.select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        ).where(lambda t: t.account_id != None)  # noqa: E711

        first_group = await base.order_by("acct").limit(1).all()
        second_group = await base.order_by("acct").limit(1).offset(1).all()

        assert first_group.model_dump() == [{"acct": 1, "total": 30}]
        assert second_group.model_dump() == [{"acct": 2, "total": 40}]


# ---------------------------------------------------------------------------
# The three order_by rules.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_by_string_resolves_output_names_before_root_columns(db_url):
    """Rule 1, pinned where the pools differ: the output field 'note' aliases
    the ACCOUNT NAME, while the root column 'note' holds other values —
    order_by("note") must sort by the output field."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"note": t.account.name, "total": t.amount.sum()}
            )
            .order_by("note", "desc")
            .all()
        )

        assert [row.note for row in rows] == ["a2", "a1"]


@pytest.mark.asyncio
async def test_order_by_lambda_spells_the_aggregate_source(db_url):
    """Rule 2: order_by(lambda t: t.amount.sum(), "desc") resolves to the
    projected aggregate and sorts by it."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.account_id != None)  # noqa: E711
            .order_by(lambda t: t.amount.sum(), "desc")
            .all()
        )

        assert [row.acct for row in rows] == [2, 1]


@pytest.mark.asyncio
async def test_order_by_lambda_group_key_source(db_url):
    """Rule 2, plain side: a lambda spelling a group key's source column
    (traversal included) sorts the groups."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            GRPTransaction.select(
                lambda t: {"account_name": t.account.name, "total": t.amount.sum()}
            )
            .order_by(lambda t: t.account.name, "desc")
            .all()
        )

        assert [row.account_name for row in rows] == ["a2", "a1"]


class TestOrderByGuardrails:
    """Rule 3 and friends: build-time, before any SQL."""

    def _grouped(self):
        return GRPTransaction.select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        )

    def test_ungrouped_bare_string_key_raises(self):
        with pytest.raises(ValueError, match=r"group key or an aggregate"):
            self._grouped().order_by("note")

    def test_ungrouped_bare_lambda_key_raises(self):
        with pytest.raises(ValueError, match=r"t\.note.*group key or an aggregate"):
            self._grouped().order_by(lambda t: t.note)

    def test_unprojected_aggregate_sort_key_raises(self):
        with pytest.raises(ValueError, match=r"matches no.*projected"):
            self._grouped().order_by(lambda t: t.price.avg())

    def test_aggregate_sort_key_on_plain_projection_raises(self):
        plain = GRPTransaction.select(lambda t: (t.id, t.amount))
        with pytest.raises(ValueError, match=r"matches no.*projected"):
            plain.order_by(lambda t: t.amount.sum())

    def test_misspelled_string_names_output_fields_too(self):
        with pytest.raises(AttributeError, match=r"output field names.*acct, total"):
            self._grouped().order_by("tolat")

    def test_ungrouped_sort_key_added_before_select_raises_too(self):
        # Rule 3 reaches sort keys chained AHEAD of the projection: the
        # select() call is where the query becomes grouped, so it validates
        # what was already ordered.
        with pytest.raises(ValueError, match=r"t\.note.*before this select"):
            GRPTransaction.select().order_by("note").select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )

    def test_group_key_sort_added_before_select_is_allowed(self):
        q = GRPTransaction.select().order_by("account_id").select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        )
        assert q is not None

    def test_group_key_source_string_is_allowed(self):
        # 'account_id' is the source column of the 'acct' group key — a valid
        # sort key even though the output name differs.
        assert self._grouped().order_by("account_id") is not None

    def test_plain_projection_keeps_unselected_column_sorting(self):
        # Rule 3 is aggregate-only: a plain projection still sorts by
        # unselected root columns (#279 behavior, unchanged).
        plain = GRPTransaction.select(lambda t: (t.id,))
        assert plain.order_by("amount") is not None


# ---------------------------------------------------------------------------
# count()/exists() guidance on aggregate projections.
# ---------------------------------------------------------------------------


class TestVerbGuardrails:
    """Synchronous raises at the call — no coroutine, no round trip."""

    def _grouped(self):
        return GRPTransaction.select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        )

    def test_count_raises_with_both_spellings(self):
        with pytest.raises(
            ValueError, match=r"unprojected query.*len\(await q\.all\(\)\)"
        ):
            self._grouped().count()

    def test_exists_raises_with_guidance(self):
        with pytest.raises(ValueError, match=r"exactly one record"):
            self._grouped().exists()

    @pytest.mark.asyncio
    async def test_count_and_exists_still_work_on_plain_projections(self, db_url):
        await ferro.connect(db_url, auto_migrate=True)
        async with ferro.engines.session():
            await _seed()
            plain = GRPTransaction.select(lambda t: (t.id,))
            assert await plain.count() == 4
            assert await plain.exists() is True
