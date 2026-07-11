"""Backend-matrix integration tests for populated relations (#286, ADR-0008).

``select().include(lambda t: t.account)`` end to end: the ``instances``
materialization plan renders the include-only LEFT join and the widened,
aliased SELECT, decodes every hop against the hop model's own codec plan, and
populates the relation per the population contract — plain attribute access
returning the complete instance, no await, no query — while unpopulated
relations keep the awaitable contract unchanged. No SQL-string snapshots at
this layer (the rendered SELECT/edge-union pins live in the Rust walker unit
tests). Covers membership preservation, the population contract, identity-map
semantics (dedup, refresh-attach), sessionless freshness, typed-column parity
vs a direct query, verb composition, and the include-vs-joins wire split.
"""

import inspect
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest

import ferro
from ferro import (
    BackRef,
    FerroField,
    ForeignKey,
    ManyToMany,
    Model,
    Relation,
    execute,
)
from ferro.query import builder as builder_module

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: Transaction -> Account -> Owner (both FKs nullable, so populated-
# `None` and mid-chain NULL are observable), a second Transaction FK
# (Category) so accumulation across includes is observable, a typed-column
# pair with a root/hop name collision for decode-parity pins, and an M2M trio
# whose target model carries a forward FK.
# ---------------------------------------------------------------------------


class INOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    accounts: Relation[list["INAccount"]] = BackRef()


class INAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    owner: Annotated[INOwner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["INTransaction"]] = BackRef()


class INCategory(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    transactions: Relation[list["INTransaction"]] = BackRef()


class INTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    note: str = ""
    account: Annotated[INAccount | None, ForeignKey(related_name="transactions")] = None
    category: Annotated[
        INCategory | None, ForeignKey(related_name="transactions")
    ] = None


class INMAuthor(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    role: str = ""
    tags: Relation[list["INMTag"]] = BackRef()


class INMTag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    created_by: Annotated[INMAuthor, ForeignKey(related_name="tags")]
    posts: Relation[list["INMPost"]] = BackRef()


class INMPost(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str = ""
    tags: Relation[list["INMTag"]] = ManyToMany(related_name="posts")


class INTier(StrEnum):
    FREE = "free"
    PRO = "pro"


class INTyProfile(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    created_at: datetime
    external_id: UUID
    balance: Decimal | None = None
    tier: INTier = INTier.FREE
    # Plain-text column; collides by name with INTyUser.tag (a native enum) —
    # the per-hop codec pin (the stage-1 root-vs-hop lesson).
    tag: str = ""
    users: Relation[list["INTyUser"]] = BackRef()


class INTyUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    tag: INTier = INTier.FREE
    profile: Annotated[INTyProfile, ForeignKey(related_name="users")]


_TY_T1 = datetime(2026, 3, 1, 12, 30, 45, 123456, tzinfo=timezone.utc)
_TY_UID1 = UUID("11111111-1111-1111-1111-111111111111")


async def _seed_core():
    """Owner o1 <- accounts a1 (2 txns) and a2 (1 txn); account b1 with no
    owner (1 txn); one transaction with no account at all (NULL FK); txn 1
    additionally categorized (the second FK, for accumulation pins)."""
    o1 = INOwner(id=1, name="o1")
    await o1.save()
    a1 = INAccount(id=1, label="a1", owner=o1)
    a2 = INAccount(id=2, label="a2", owner=o1)
    b1 = INAccount(id=3, label="b1", owner=None)
    c1 = INCategory(id=1, name="groceries")
    for row in (a1, a2, b1, c1):
        await row.save()
    for txn in (
        INTransaction(id=1, amount=10, note="n1", account=a1, category=c1),
        INTransaction(id=2, amount=20, note="n2", account=a1),
        INTransaction(id=3, amount=30, note="n3", account=a2),
        INTransaction(id=4, amount=40, note="n4", account=b1),
        INTransaction(id=5, amount=50, note="n5", account=None),
    ):
        await txn.save()
    return {"o1": o1, "a1": a1, "a2": a2, "b1": b1, "c1": c1}


async def _seed_typed():
    profile = INTyProfile(
        id=1,
        created_at=_TY_T1,
        external_id=_TY_UID1,
        balance=Decimal("12.50"),
        tier=INTier.PRO,
        tag="vip",
    )
    await profile.save()
    await INTyUser(id=1, tag=INTier.PRO, profile=profile).save()


def _ph(db_url: str, n: int = 1) -> str:
    """Return the Nth positional placeholder for the active backend."""
    return f"${n}" if "postgres" in db_url else "?"


# ---------------------------------------------------------------------------
# The tracer bullet: populated plain-attribute access, one statement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_populates_relation_as_plain_attribute(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txns = await (
            INTransaction.select().include(lambda t: t.account).order_by("id").all()
        )

        assert isinstance(txns, list)
        assert all(isinstance(t, INTransaction) for t in txns)
        # Plain attribute — a complete instance, not a coroutine.
        assert isinstance(txns[0].account, INAccount)
        assert txns[0].account.label == "a1"
        assert txns[2].account.label == "a2"
        assert txns[3].account.label == "b1"


@pytest.mark.asyncio
async def test_include_preserves_membership_and_populates_null_fk_as_none(db_url):
    """Adding .include(...) returns exactly the rows the query returned
    without it; a NULL nullable FK populates as None with the root retained."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        plain = await INTransaction.select().order_by("id").all()
        included = await (
            INTransaction.select().include(lambda t: t.account).order_by("id").all()
        )

        assert [t.id for t in included] == [t.id for t in plain] == [1, 2, 3, 4, 5]
        orphan = included[4]
        # Populated-`None`: a plain attribute, truthful to the declared
        # `| None` type — not an awaitable.
        assert orphan.account is None


@pytest.mark.asyncio
async def test_unpopulated_relations_keep_the_awaitable_contract(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await INTransaction.select().where(lambda t: t.id == 1).first()
        assert txn is not None

        pending = txn.account
        assert inspect.iscoroutine(pending), "unpopulated access stays awaitable"
        account = await pending
        assert isinstance(account, INAccount) and account.label == "a1"


# ---------------------------------------------------------------------------
# Identity map: same object as a direct fetch, deduped across rows,
# refresh-attached onto instances the session already holds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populated_instances_are_identity_mapped_and_deduped(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        direct = await INAccount.get(1)
        txns = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.amount <= 20)
            .order_by("id")
            .all()
        )

        assert len(txns) == 2
        # Same row, same object: across result rows AND with the instance the
        # session already held.
        assert txns[0].account is txns[1].account
        assert txns[0].account is direct


@pytest.mark.asyncio
async def test_include_refresh_attaches_onto_a_live_instance(db_url):
    """A later include query refreshes the session's existing instance in
    place (full hop.* row) and attaches it — shared objects get richer,
    never forked."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        held = await INAccount.get(1)
        # External write the ORM cache knows nothing about.
        await execute(
            f"UPDATE inaccount SET label = {_ph(db_url, 1)} "
            f"WHERE id = {_ph(db_url, 2)}",
            "a1-renamed",
            1,
        )

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )

        assert txn is not None
        assert txn.account is held, "population reuses the held instance"
        assert held.label == "a1-renamed", "the hit refreshed the held instance"


@pytest.mark.asyncio
async def test_sessionless_include_returns_fresh_instances_per_row(db_url):
    """Sessionless Ferro keeps exactly one identity story: no session, no
    identity map, no query-local dedup — fresh instances per row."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

    txns = await (
        INTransaction.using("default")
        .select()
        .include(lambda t: t.account)
        .where(lambda t: t.amount <= 20)
        .order_by("id")
        .all()
    )

    assert len(txns) == 2
    assert isinstance(txns[0].account, INAccount)
    assert txns[0].account is not txns[1].account
    assert txns[0].account.id == txns[1].account.id == 1


# ---------------------------------------------------------------------------
# Typed-column decode parity: per-hop decode uses the hop model's codec plan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populated_typed_columns_equal_a_direct_query(db_url):
    """datetime / uuid / native enum / decimal on a populated instance match a
    direct query's instance in type and value on both backends — even with a
    root/hop column-name collision (INTyUser.tag enum vs INTyProfile.tag
    text), so the hop can only have decoded through its OWN codec plan."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed()

    # Sessionless on purpose: distinct objects, so equality is decode parity,
    # not identity.
    user = await INTyUser.using("default").select().include(lambda u: u.profile).first()
    direct = await INTyProfile.using("default").select().first()

    assert user is not None and direct is not None
    populated = user.profile
    assert isinstance(populated, INTyProfile)
    assert populated is not direct
    for field in ("id", "created_at", "external_id", "balance", "tier", "tag"):
        hydrated = getattr(direct, field)
        via_include = getattr(populated, field)
        assert type(via_include) is type(hydrated), field
        assert via_include == hydrated, field
    assert isinstance(populated.created_at, datetime)
    assert isinstance(populated.external_id, UUID)
    assert isinstance(populated.tier, INTier) and populated.tier is INTier.PRO
    assert isinstance(populated.balance, Decimal) and populated.balance == Decimal(
        "12.50"
    )
    # The root's same-named enum column still decodes as the root's enum.
    assert user.tag is INTier.PRO
    assert isinstance(populated.tag, str) and populated.tag == "vip"


# ---------------------------------------------------------------------------
# Interplay with stage-1 joins: include never changes membership or a shared
# edge's semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_keeps_traversal_predicate_semantics(db_url):
    """A where() traversal on the same path keeps its INNER semantics — the
    row set is identical with and without the include — and the surviving
    rows come back populated."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        plain = await (
            INTransaction.select()
            .where(lambda t: t.account.label == "a1")
            .order_by("id")
            .all()
        )
        included = await (
            INTransaction.select()
            .where(lambda t: t.account.label == "a1")
            .include(lambda t: t.account)
            .order_by("id")
            .all()
        )

        assert [t.id for t in included] == [t.id for t in plain] == [1, 2]
        assert all(t.account.label == "a1" for t in included)


@pytest.mark.asyncio
async def test_include_keeps_order_by_traversal_semantics(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        plain = await (
            INTransaction.select()
            .order_by(lambda t: t.account.label, "desc")
            .order_by("id")
            .all()
        )
        included = await (
            INTransaction.select()
            .order_by(lambda t: t.account.label, "desc")
            .order_by("id")
            .include(lambda t: t.account)
            .all()
        )

        assert [t.id for t in included] == [t.id for t in plain]
        assert included[0].account.label == "b1"


@pytest.mark.asyncio
async def test_include_composes_with_explicit_left_join_on_the_same_path(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txns = await (
            INTransaction.select()
            .left_join(lambda t: t.account)
            .include(lambda t: t.account)
            .order_by("id")
            .all()
        )

        assert [t.id for t in txns] == [1, 2, 3, 4, 5], "LEFT keeps orphans"
        assert txns[0].account.label == "a1"
        assert txns[4].account is None


# ---------------------------------------------------------------------------
# Verb composition: first(), count(), exists(), pagination, immutability.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_returns_a_populated_instance_or_none(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        q = INTransaction.select().include(lambda t: t.account)
        first = await q.order_by("amount", "desc").where(lambda t: t.id <= 4).first()
        assert first is not None and first.account.label == "b1"

        empty = await q.where(lambda t: t.amount > 1000).first()
        assert empty is None


@pytest.mark.asyncio
async def test_count_and_exists_are_unaffected_by_include(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        plain = INTransaction.select().where(lambda t: t.amount >= 20)
        included = plain.include(lambda t: t.account)
        assert await included.count() == await plain.count() == 4
        assert await included.exists() is True


@pytest.mark.asyncio
async def test_count_payload_stays_root_instances_under_include(db_url, monkeypatch):
    """count() measures rows, not attached data: its payload emits
    root_instances and no include joins — pinned on the wire."""
    captured = {}

    async def _capture(name, query_ir_json, route):
        captured["envelope"] = json.loads(query_ir_json)
        return 0

    monkeypatch.setattr(builder_module, "count_filtered", _capture)
    await ferro.connect(db_url, auto_migrate=True)

    await INTransaction.using("default").select().include(lambda t: t.account).count()

    payload = captured["envelope"]["payload"]
    assert payload["materialization"] == {"kind": "root_instances"}
    assert payload["joins"] == []


@pytest.mark.asyncio
async def test_include_composes_with_limit_offset(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txns = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .order_by("id")
            .limit(2)
            .offset(1)
            .all()
        )
        assert [t.id for t in txns] == [2, 3]
        assert txns[1].account.label == "a2"


def test_include_is_immutable_and_idempotent():
    base = INTransaction.select()
    included = base.include(lambda t: t.account)
    again = included.include(lambda t: t.account)

    assert base._includes == {}
    assert list(included._includes) == [("account",)]
    assert list(again._includes) == [("account",)], "same path dedups"
    assert included is not base and again is not included


# ---------------------------------------------------------------------------
# The wire split (ADR-0008): include paths ride the instances plan, never the
# joins section.
# ---------------------------------------------------------------------------


def test_include_paths_never_enter_the_joins_wire_section():
    q = INTransaction.select().include(lambda t: t.account)

    assert q._joins == {}
    assert q._serialize_joins() == []
    assert q._materialization_ir() == {
        "kind": "instances",
        "paths": [
            [
                {
                    "relation": "account",
                    "from_column": "account_id",
                    "to_table": "inaccount",
                    "to_column": "id",
                }
            ]
        ],
    }


def test_include_selector_validation():
    with pytest.raises(AttributeError, match="Did you mean 'account'"):
        INTransaction.select().include(lambda t: t.acount)
    with pytest.raises(TypeError, match="column 'account.label', not a relation"):
        INTransaction.select().include(lambda t: t.account.label)
    with pytest.raises(TypeError, match="must return a relation path"):
        INTransaction.select().include(lambda t: 42)
    with pytest.raises(TypeError, match="selector callable"):
        INTransaction.select().include(42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# ---------------------------------------------------------------------------
# Multi-hop (#287): whole-path population, prefix dedup, order freedom, NULL
# mid-chain.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_hop_include_populates_every_hop(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account.owner)
            .where(lambda t: t.id == 1)
            .first()
        )

        assert txn is not None
        # Whole-path population: a populated graph is never missing its
        # intermediate nodes.
        assert isinstance(txn.account, INAccount) and txn.account.label == "a1"
        assert isinstance(txn.account.owner, INOwner) and txn.account.owner.name == "o1"


@pytest.mark.asyncio
async def test_shared_prefix_includes_dedup_and_are_order_free(db_url):
    """`.include(account)` + `.include(account.owner)` — in either chain
    order — populates exactly what `.include(account.owner)` alone does."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        base = INTransaction.select().where(lambda t: t.id == 1)
        variants = (
            base.include(lambda t: t.account).include(lambda t: t.account.owner),
            base.include(lambda t: t.account.owner).include(lambda t: t.account),
            base.include(lambda t: t.account.owner),
        )
        for q in variants:
            txn = await q.first()
            assert txn is not None
            assert txn.account.label == "a1"
            assert txn.account.owner.name == "o1"


@pytest.mark.asyncio
async def test_null_mid_chain_ends_the_chain_as_populated_none(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txns = await (
            INTransaction.select()
            .include(lambda t: t.account.owner)
            .order_by("id")
            .all()
        )

        assert [t.id for t in txns] == [1, 2, 3, 4, 5], "roots retained"
        # b1 has no owner: hop 1 populated, hop 2 populated-None.
        ownerless = txns[3]
        assert ownerless.account.label == "b1"
        assert ownerless.account.owner is None
        # No account at all: hop 1 populated-None, the chain ends there.
        orphan = txns[4]
        assert orphan.account is None


# ---------------------------------------------------------------------------
# The refresh rule (#287, ADR-0008): keep iff the FK still matches; drop on
# change; accumulate across a session's queries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_population_survives_a_refresh_with_unchanged_fk(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )
        assert txn is not None and txn.account.label == "a1"

        again = await INTransaction.get(1)

        assert again is txn, "refresh hit the same identity-mapped object"
        assert isinstance(txn.account, INAccount), "population survived"
        assert txn.account.label == "a1"


@pytest.mark.asyncio
async def test_populations_accumulate_across_a_sessions_queries(db_url):
    """An account include and a later category include both stick: queries
    compose instead of clobbering each other."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        first = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )
        second = await (
            INTransaction.select()
            .include(lambda t: t.category)
            .where(lambda t: t.id == 1)
            .first()
        )

        assert second is first
        assert isinstance(first.account, INAccount), "earlier population kept"
        assert isinstance(first.category, INCategory), "later population added"
        assert first.category.name == "groceries"


@pytest.mark.asyncio
async def test_refresh_drops_population_when_the_fk_changed(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )
        assert txn is not None and txn.account.id == 1

        # External write: the row now points at a different account.
        await execute(
            f"UPDATE intransaction SET account_id = {_ph(db_url, 1)} "
            f"WHERE id = {_ph(db_url, 2)}",
            2,
            1,
        )
        again = await INTransaction.get(1)
        assert again is txn

        # The population would lie now — it was dropped, so access reverts
        # to the awaitable and resolves to the CURRENT row.
        pending = txn.account
        assert inspect.iscoroutine(pending)
        fresh = await pending
        assert fresh.id == 2


@pytest.mark.asyncio
async def test_refresh_drops_population_when_the_fk_went_null(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )
        assert txn is not None and isinstance(txn.account, INAccount)

        await execute(
            f"UPDATE intransaction SET account_id = NULL WHERE id = {_ph(db_url, 1)}",
            1,
        )
        again = await INTransaction.get(1)
        assert again is txn

        pending = txn.account
        assert inspect.iscoroutine(pending), "population dropped on NULLed FK"
        assert await pending is None


@pytest.mark.asyncio
async def test_populated_none_survives_while_the_fk_stays_null(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        txn = await (
            INTransaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 5)
            .first()
        )
        assert txn is not None and txn.account is None

        again = await INTransaction.get(5)
        assert again is txn
        assert txn.account is None, "populated-None kept: the FK is still NULL"


# ---------------------------------------------------------------------------
# M2M association context (#287): include composes with `post.tags`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m2m_association_query_composes_with_include(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        author = INMAuthor(id=1, role="editor")
        await author.save()
        t1 = INMTag(id=1, name="rust", created_by=author)
        t2 = INMTag(id=2, name="python", created_by=author)
        for tag in (t1, t2):
            await tag.save()
        post = INMPost(id=1, title="ferro")
        await post.save()
        await post.tags.add(t1, t2)

        tags = await post.tags.include(lambda tag: tag.created_by).order_by("id").all()

        assert [t.name for t in tags] == ["rust", "python"]
        assert all(isinstance(t.created_by, INMAuthor) for t in tags)
        assert tags[0].created_by is tags[1].created_by, "identity-mapped"
        assert tags[0].created_by.role == "editor"


# ---------------------------------------------------------------------------
# Guardrails (#287): loud, pointed, before any round-trip.
# ---------------------------------------------------------------------------


def test_string_include_selector_names_the_lambda_form():
    with pytest.raises(
        TypeError,
        match=r"not a string.*include\(lambda t: t\.account\)",
    ):
        INTransaction.select().include("account")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_backref_include_selector_names_the_future_mechanism(db_url):
    # connect() resolves relationships, injecting the reverse descriptors the
    # guardrail identifies.
    await ferro.connect(db_url, auto_migrate=True)
    with pytest.raises(
        TypeError,
        match=r"reverse \(BackRef\) or many-to-many.*batched second query",
    ):
        INAccount.select().include(lambda a: a.transactions)


@pytest.mark.asyncio
async def test_m2m_include_selector_names_the_future_mechanism(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    with pytest.raises(
        TypeError,
        match=r"reverse \(BackRef\) or many-to-many.*batched second query",
    ):
        INMPost.select().include(lambda p: p.tags)


def test_include_and_projection_raise_in_both_chain_orders():
    with pytest.raises(ValueError, match=r"exactly one materialization plan.*#282"):
        INTransaction.select().include(lambda t: t.account).select(
            lambda t: (t.id, t.amount)
        )
    with pytest.raises(ValueError, match=r"exactly one materialization plan.*#282"):
        INTransaction.select(lambda t: (t.id, t.amount)).include(  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            lambda t: t.account
        )


@pytest.mark.asyncio
async def test_mutations_on_an_included_query_raise_before_any_round_trip():
    """No ferro.connect in scope: the build-time guard fires before any
    route or SQL exists."""
    included = INTransaction.select().include(lambda t: t.account)
    with pytest.raises(ValueError, match=r"update\(\) does not support include\(\)"):
        await included.update(note="x")
    with pytest.raises(ValueError, match=r"delete\(\) does not support include\(\)"):
        await included.delete()
