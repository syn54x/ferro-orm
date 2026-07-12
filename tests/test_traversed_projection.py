"""Backend-matrix integration tests for traversed projection + output aliases
(#293, ADR-0009 on the ADR-0006/ADR-0007 substrate).

``select()`` reaches across relations, end to end: lambda selectors traverse
forward-FK paths at any depth (``select(lambda t: (t.amount,
t.account.name))``), and the dict-returning lambda names output fields
(``select(lambda t: {"account_name": t.account.name})`` — CONTEXT.md: Output
alias, Traversed projection). Covers depth-2 traversal in both selector
forms, decode parity vs full hydration, INNER-narrowing and the ``left_join``
opt-out (rows kept, ``None`` fields, NULL-tolerant decode), shared-path join
identity (behavioral — the rendered-SQL pins live in the Rust walker unit
tests), and the build-time error catalog: output-name collision naming the
dict form, mixed nesting, non-string dict keys.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro.query import Row, Rows

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: Transaction -> Account -> Owner (depth 2), typed hop columns.
# ---------------------------------------------------------------------------


class TPTier(StrEnum):
    FREE = "free"
    PRO = "pro"


class TPOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: str = ""
    tier: TPTier = TPTier.FREE
    joined_at: datetime | None = None
    external_id: UUID | None = None
    accounts: Relation[list["TPAccount"]] = BackRef()


class TPAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    # Deliberately non-nullable: the left_join tests pin that a kept row's
    # traversed field decodes to None even from a NOT NULL source column.
    name: str = ""
    balance: Decimal | None = None
    owner: Annotated[TPOwner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["TPTransaction"]] = BackRef()


class TPTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[
        TPAccount | None, ForeignKey(related_name="transactions")
    ] = None


_JOINED = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone.utc)
_OWNER_UID = UUID("22222222-2222-2222-2222-222222222222")


async def _seed():
    """Owner alice <- account a1 <- txns 1/2 (10, 20); txn 3 (30) has NO
    account (NULL FK); account a2 (owner bob) <- txn 4 (40)."""
    alice = TPOwner(
        id=1, email="alice@x.io", tier=TPTier.PRO, joined_at=_JOINED,
        external_id=_OWNER_UID,
    )
    bob = TPOwner(id=2, email="bob@x.io", tier=TPTier.FREE)
    await alice.save()
    await bob.save()
    a1 = TPAccount(id=1, name="a1", balance=Decimal("12.50"), owner=alice)
    a2 = TPAccount(id=2, name="a2", owner=bob)
    await a1.save()
    await a2.save()
    await TPTransaction(id=1, amount=10, account=a1).save()
    await TPTransaction(id=2, amount=20, account=a1).save()
    await TPTransaction(id=3, amount=30).save()
    await TPTransaction(id=4, amount=40, account=a2).save()


# ---------------------------------------------------------------------------
# The tracer bullet: tuple and dict traversal at depth >= 2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tuple_traversal_at_depth_two_takes_bare_leaf_names(db_url):
    """Unaliased traversed fields take the bare leaf column name, in
    selection order, at depth 1 and 2."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(
                lambda t: (t.amount, t.account.name, t.account.owner.email)
            )
            .order_by("id")
            .all()
        )

        assert isinstance(rows, Rows)
        assert list(rows[0].model_dump()) == ["amount", "name", "email"]
        assert [(r.amount, r.name, r.email) for r in rows] == [
            (10, "a1", "alice@x.io"),
            (20, "a1", "alice@x.io"),
            (40, "a2", "bob@x.io"),
        ]


@pytest.mark.asyncio
async def test_dict_selector_names_output_fields_in_insertion_order(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(
                lambda t: {
                    "txn_id": t.id,
                    "account_name": t.account.name,
                    "owner_email": t.account.owner.email,
                }
            )
            .order_by("id")
            .all()
        )

        assert list(rows[0].model_dump()) == ["txn_id", "account_name", "owner_email"]
        assert rows.model_dump() == [
            {"txn_id": 1, "account_name": "a1", "owner_email": "alice@x.io"},
            {"txn_id": 2, "account_name": "a1", "owner_email": "alice@x.io"},
            {"txn_id": 4, "account_name": "a2", "owner_email": "bob@x.io"},
        ]


@pytest.mark.asyncio
async def test_aliased_root_field_renders_under_output_name(db_url):
    """An output alias on a plain root column: same value, new name."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            TPTransaction.select(lambda t: {"total": t.amount}).order_by("id").first()
        )

        assert row is not None
        assert row.model_dump() == {"total": 10}


@pytest.mark.asyncio
async def test_same_leaf_selected_under_two_aliases(db_url):
    """The dict form disambiguates what unaliased selection cannot: the same
    output-colliding leaves (t.id vs t.account.id) under distinct names."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            TPTransaction.select(lambda t: {"txn_id": t.id, "account_id": t.account.id})
            .order_by("id")
            .first()
        )

        assert row is not None
        assert row.model_dump() == {"txn_id": 1, "account_id": 1}


# ---------------------------------------------------------------------------
# Decode parity: traversed values decode identically to full hydration.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traversed_typed_columns_decode_identically_to_full_hydration(db_url):
    """datetime / uuid / native enum / decimal across one and two hops come
    back with the same types and values as hydrating the related models."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        owner = await TPOwner.where(lambda o: o.id == 1).first()
        account = await TPAccount.where(lambda a: a.id == 1).first()
        row = await (
            TPTransaction.select(
                lambda t: {
                    "balance": t.account.balance,
                    "tier": t.account.owner.tier,
                    "joined_at": t.account.owner.joined_at,
                    "external_id": t.account.owner.external_id,
                }
            )
            .where(lambda t: t.id == 1)
            .first()
        )

        assert owner is not None and account is not None and row is not None
        assert type(row.balance) is type(account.balance)
        assert row.balance == account.balance == Decimal("12.50")
        assert isinstance(row.tier, TPTier) and row.tier is owner.tier is TPTier.PRO
        assert type(row.joined_at) is type(owner.joined_at)
        assert row.joined_at == owner.joined_at
        assert isinstance(row.external_id, UUID)
        assert row.external_id == owner.external_id == _OWNER_UID


# ---------------------------------------------------------------------------
# INNER-narrowing and the left_join opt-out (ADR-0006).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_traversal_narrows_like_predicate_traversal(db_url):
    """Traversing a nullable relation in the projection drops relation-less
    rows — txn 3 (NULL FK) disappears, exactly like a where() traversal."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(lambda t: (t.id, t.account.name)).order_by("id").all()
        )

        assert [r.id for r in rows] == [1, 2, 4]


@pytest.mark.asyncio
async def test_left_join_keeps_rows_and_decodes_traversed_fields_to_none(db_url):
    """The opt-out: left_join keeps relation-less rows; their traversed
    fields decode to None — including from the NOT NULL source column
    ``TPAccount.name`` (a projected record is not the related model)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(lambda t: {"id": t.id, "account_name": t.account.name})
            .left_join(lambda t: t.account)
            .order_by("id")
            .all()
        )

        assert [r.id for r in rows] == [1, 2, 3, 4]
        by_id = {r.id: r.account_name for r in rows}
        assert by_id[3] is None
        assert by_id[1] == "a1" and by_id[4] == "a2"


@pytest.mark.asyncio
async def test_left_join_whole_path_none_at_depth_two(db_url):
    """A LEFT-marked path keeps rows missing the relation at any hop, and
    every traversed field along it decodes to None — enum and datetime
    columns included (NULL-tolerant decode)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(
                lambda t: {
                    "id": t.id,
                    "owner_email": t.account.owner.email,
                    "owner_tier": t.account.owner.tier,
                    "owner_joined": t.account.owner.joined_at,
                }
            )
            .left_join(lambda t: t.account.owner)
            .order_by("id")
            .all()
        )

        assert [r.id for r in rows] == [1, 2, 3, 4]
        orphan = next(r for r in rows if r.id == 3)
        assert orphan.owner_email is None
        assert orphan.owner_tier is None
        assert orphan.owner_joined is None
        kept = next(r for r in rows if r.id == 1)
        assert kept.owner_email == "alice@x.io" and kept.owner_tier is TPTier.PRO


@pytest.mark.asyncio
async def test_projection_shares_join_identity_with_where_traversal(db_url):
    """The same path in select() and where() is ONE join (ADR-0006: the path
    is the join identity) — narrowing applies once, values stay coherent.
    The single-JOIN SQL pin lives in the Rust walker unit tests."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        rows = await (
            TPTransaction.select(lambda t: (t.id, t.account.name))
            .where(lambda t: t.account.name == "a1")
            .order_by("id")
            .all()
        )

        assert [(r.id, r.name) for r in rows] == [(1, "a1"), (2, "a1")]


@pytest.mark.asyncio
async def test_left_join_beats_projection_inner_on_shared_path(db_url):
    """An explicit left_join wins over the projection's implicit INNER on the
    shared edge — the ADR-0006 conflict rule extends to projection traversal."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        # Projection traverses account (implicit INNER); left_join overrides.
        rows = await (
            TPTransaction.select(lambda t: (t.id, t.account.name))
            .left_join(lambda t: t.account)
            .order_by("id")
            .all()
        )

        assert [r.id for r in rows] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Build-time error catalog.
# ---------------------------------------------------------------------------


class TestBuildTimeErrors:
    def test_output_name_collision_names_the_dict_form(self):
        with pytest.raises(ValueError, match=r"two fields named 'id'.*dict"):
            TPTransaction.select(lambda t: (t.id, t.account.id))

    def test_root_duplicate_is_the_same_collision(self):
        with pytest.raises(ValueError, match=r"two fields named 'id'"):
            TPTransaction.select(lambda t: (t.id, t.id))

    def test_dict_in_tuple_raises(self):
        with pytest.raises(TypeError, match="cannot nest a dict inside a tuple"):
            TPTransaction.select(lambda t: (t.id, {"name": t.account.name}))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_tuple_in_dict_raises(self):
        with pytest.raises(TypeError, match=r"'pair' is a tuple.*flat"):
            TPTransaction.select(lambda t: {"pair": (t.id, t.amount)})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_dict_in_dict_raises(self):
        with pytest.raises(TypeError, match=r"'nested' is a dict.*flat"):
            TPTransaction.select(lambda t: {"nested": {"id": t.id}})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_non_string_dict_key_raises(self):
        with pytest.raises(TypeError, match="must be strings, got 1"):
            TPTransaction.select(lambda t: {1: t.id})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError, match="selected no columns"):
            TPTransaction.select(lambda t: {})

    def test_bare_relation_in_dict_raises(self):
        with pytest.raises(TypeError, match="bare relation 'account'"):
            TPTransaction.select(lambda t: {"acct": t.account})

    def test_comparison_in_dict_raises(self):
        with pytest.raises(TypeError, match="comparison"):
            TPTransaction.select(lambda t: {"big": t.amount >= 10})

    def test_misspelled_traversed_column_names_the_hop_model(self):
        with pytest.raises(AttributeError, match="TPAccount.*Did you mean 'name'"):
            TPTransaction.select(lambda t: t.account.nmae)

    def test_include_then_select_final_error_points_at_traversed_projection(self):
        with pytest.raises(
            ValueError, match=r"record results are flat.*traversed projection"
        ):
            TPTransaction.select().include(lambda t: t.account).select(
                lambda t: (t.id,)
            )

    def test_select_then_include_final_error_points_at_traversed_projection(self):
        with pytest.raises(
            ValueError, match=r"record results are flat.*traversed projection"
        ):
            TPTransaction.select(lambda t: (t.id,)).include(lambda t: t.account)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_count_reflects_projection_traversals_narrowing(db_url):
    """count()/exists() stay projection-blind but join-aware: the traversed
    path's INNER join narrows membership (ADR-0006 — joins decide membership,
    projection decides shape), so count() equals len(all())."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        narrowed = TPTransaction.select(lambda t: (t.id, t.account.name))
        assert await narrowed.count() == 3 == len(await narrowed.all())

        kept = TPTransaction.select(lambda t: (t.id, t.account.name)).left_join(
            lambda t: t.account
        )
        assert await kept.count() == 4 == len(await kept.all())


# ---------------------------------------------------------------------------
# Records stay records: no persistence surface on traversed rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traversed_rows_are_records_not_instances(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed()

        row = await (
            TPTransaction.select(lambda t: {"account_name": t.account.name}).first()
        )

        assert isinstance(row, Row)
        assert not isinstance(row, Model)
        assert not hasattr(row, "save")
