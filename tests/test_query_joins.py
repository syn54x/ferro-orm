"""Backend-matrix integration tests for relation traversal in where() and
order_by() (#270, #271).

Declares FK-related models, inserts rows, and asserts result sets against real
SQLite and isolated-schema Postgres backends — no SQL-string snapshots at this
layer (the rendered-SQL pins live in the Rust walker unit tests). Covers the
motivating Pinch query, multi-hop traversal, per-hop did-you-mean, INNER-drops-
NULL semantics (ADR-0006), join dedup, two-FKs-to-one-target, self-FK, verb
composition, query immutability, and order_by traversal sharing joins with
where() (#271).
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, ManyToMany, Model, Relation
from ferro.query import Query, QueryNode
from ferro.query.nodes import FieldProxy, QueryProxy
from ferro.query.wire import compile_query

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# Schema: Transaction -> Account -> {Ledger, Owner}; plus Transfer (two FKs to
# Account), Employee (self-FK), and a nullable-FK Note.
# ---------------------------------------------------------------------------


class QJLedger(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    accounts: Relation[list["QJAccount"]] = BackRef()


class QJOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: str = ""
    accounts: Relation[list["QJAccount"]] = BackRef()


class QJAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    ledger: Annotated[QJLedger, ForeignKey(related_name="accounts")]
    owner: Annotated[QJOwner, ForeignKey(related_name="accounts")]
    transactions: Relation[list["QJTransaction"]] = BackRef()
    transfers_out: Relation[list["QJTransfer"]] = BackRef()
    transfers_in: Relation[list["QJTransfer"]] = BackRef()
    notes: Relation[list["QJNote"]] = BackRef()


class QJTransaction(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[QJAccount, ForeignKey(related_name="transactions")]


class QJTransfer(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    from_account: Annotated[QJAccount, ForeignKey(related_name="transfers_out")]
    to_account: Annotated[QJAccount, ForeignKey(related_name="transfers_in")]


class QJEmployee(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    manager: Annotated["QJEmployee", ForeignKey(related_name="reports", nullable=True)] = (
        None
    )
    reports: Relation[list["QJEmployee"]] = BackRef()


class QJNote(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    body: str = ""
    account: Annotated[QJAccount | None, ForeignKey(related_name="notes")] = None


# Nullable-FK chain at BOTH hops (Doc -> Folder -> FolderOwner) so whole-path
# LEFT retention is observable for rows missing the relation at either hop
# (#272 acceptance criterion 3).


class QJFolderOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    folders: Relation[list["QJFolder"]] = BackRef()


class QJFolder(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""
    owner: Annotated[QJFolderOwner | None, ForeignKey(related_name="folders")] = None
    docs: Relation[list["QJDoc"]] = BackRef()


class QJDoc(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str = ""
    folder: Annotated[QJFolder | None, ForeignKey(related_name="docs")] = None


# M2M pair whose TARGET model carries a forward FK, so an M2M association
# query can traverse it: `post.tags.where(lambda t: t.created_by.role == ...)`
# (#273 M2M-composition acceptance criterion).


class QJMAuthor(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    role: str = ""
    tags: Relation[list["QJMTag"]] = BackRef()


class QJMTag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    created_by: Annotated[QJMAuthor, ForeignKey(related_name="tags")]
    posts: Relation[list["QJMPost"]] = BackRef()


class QJMPost(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str = ""
    tags: Relation[list["QJMTag"]] = ManyToMany(related_name="posts")


# Typed-bind matrix (#270 critical): a related model carrying datetime, uuid,
# decimal, native-enum, and nullable columns, reached by traversal from a root
# model whose `tag` column collides by NAME with the related `tag` but is a
# DIFFERENT type (native enum on the root, plain text on the related). These pin
# that a traversed leaf's typed bind resolves against its OWN table's model, not
# the root's codec plan / enum catalog.


class QJTBTier(StrEnum):
    FREE = "free"
    PRO = "pro"


class QJTBProfile(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    created_at: datetime
    external_id: UUID
    balance: Decimal | None = None
    tier: QJTBTier = QJTBTier.FREE
    nickname: str | None = None
    # Plain-text column; collides by name with QJTBUser.tag (a native enum).
    tag: str = ""
    users: Relation[list["QJTBUser"]] = BackRef()


class QJTBUser(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    # Native-enum column named identically to the related plain-text QJTBProfile.tag.
    tag: QJTBTier = QJTBTier.FREE
    profile: Annotated[QJTBProfile, ForeignKey(related_name="users")]


_TB_T1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
_TB_T2 = datetime(2030, 6, 15, tzinfo=timezone.utc)
_TB_UID1 = UUID("11111111-1111-1111-1111-111111111111")
_TB_UID2 = UUID("22222222-2222-2222-2222-222222222222")


async def _seed_typed_bind_matrix():
    """Two profiles with distinct typed values, each owned by one user.

    P1 (user U1): created_at=T1, external_id=UID1, balance=100, tier=PRO,
        nickname="alice", tag="vip".
    P2 (user U2): created_at=T2, external_id=UID2, balance=10, tier=FREE,
        nickname=None, tag="basic".
    """
    p1 = QJTBProfile(
        id=1,
        created_at=_TB_T1,
        external_id=_TB_UID1,
        balance=Decimal("100.00"),
        tier=QJTBTier.PRO,
        nickname="alice",
        tag="vip",
    )
    p2 = QJTBProfile(
        id=2,
        created_at=_TB_T2,
        external_id=_TB_UID2,
        balance=Decimal("10.00"),
        tier=QJTBTier.FREE,
        nickname=None,
        tag="basic",
    )
    for row in (p1, p2):
        await row.save()
    await QJTBUser(id=1, tag=QJTBTier.PRO, profile=p1).save()
    await QJTBUser(id=2, tag=QJTBTier.FREE, profile=p2).save()
    return {"p1": p1, "p2": p2}


async def _seed_core():
    """Two ledgers, two owners, accounts, and fan-in transactions.

    Ledger A has accounts a1 (owner o1) and a2 (owner o2) with 3 + 1 = 4
    transactions; Ledger B has account b1 (owner o1) with 2 transactions.
    """
    la, lb = QJLedger(id=1, name="ledger-a"), QJLedger(id=2, name="ledger-b")
    o1, o2 = QJOwner(id=1, email="o1@ferro.dev"), QJOwner(id=2, email="o2@ferro.dev")
    for row in (la, lb, o1, o2):
        await row.save()

    a1 = QJAccount(id=1, label="a1", ledger=la, owner=o1)
    a2 = QJAccount(id=2, label="a2", ledger=la, owner=o2)
    b1 = QJAccount(id=3, label="b1", ledger=lb, owner=o1)
    for row in (a1, a2, b1):
        await row.save()

    # Ledger A: 4 transactions (a1 x3, a2 x1); Ledger B: 2 transactions (b1).
    for txn in (
        QJTransaction(id=1, amount=10, account=a1),
        QJTransaction(id=2, amount=20, account=a1),
        QJTransaction(id=3, amount=30, account=a1),
        QJTransaction(id=4, amount=40, account=a2),
        QJTransaction(id=5, amount=50, account=b1),
        QJTransaction(id=6, amount=60, account=b1),
    ):
        await txn.save()
    return {"la": la, "lb": lb, "o1": o1, "o2": o2, "a1": a1, "a2": a2, "b1": b1}


# ---------------------------------------------------------------------------
# Acceptance: the motivating Pinch query + multi-hop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinch_query_filters_through_relation(db_url):
    """`t.account.ledger_id == lid` composed with a root-column range filter,
    ordered by a root column, limited — one statement, right rows, right order."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        rows = await (
            QJTransaction.where(lambda t: t.account.ledger_id == 1)
            .where(lambda t: t.amount >= 20)
            .order_by(lambda t: t.amount, "desc")
            .limit(2)
            .all()
        )
        # Ledger A transactions with amount >= 20: 40, 30, 20 -> desc, top 2.
        assert [r.amount for r in rows] == [40, 30]


@pytest.mark.asyncio
async def test_multi_hop_traversal(db_url):
    """`t.account.owner.email == x` traverses two hops in one statement."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        rows = await QJTransaction.where(
            lambda t: t.account.owner.email == "o2@ferro.dev"
        ).all()
        # Only account a2 has owner o2, and a2 has one transaction (id=4).
        assert {r.id for r in rows} == {4}


# ---------------------------------------------------------------------------
# Per-hop did-you-mean (build time, no DB round-trip).
# ---------------------------------------------------------------------------


class TestPerHopDidYouMean:
    def test_misspelled_relation_at_root(self):
        with pytest.raises(AttributeError) as exc:
            QJTransaction.where(lambda t: t.accont.ledger_id == 1)
        msg = str(exc.value)
        assert "QJTransaction" in msg and "account" in msg

    def test_misspelled_column_at_hop_one(self):
        with pytest.raises(AttributeError) as exc:
            QJTransaction.where(lambda t: t.account.labl == "x")
        msg = str(exc.value)
        # Error names the hop's model (Account) and suggests the real column.
        assert "QJAccount" in msg and "label" in msg

    def test_misspelled_anything_at_hop_two(self):
        with pytest.raises(AttributeError) as exc:
            QJTransaction.where(lambda t: t.account.owner.emial == "x")
        msg = str(exc.value)
        assert "QJOwner" in msg and "email" in msg


# ---------------------------------------------------------------------------
# INNER at every hop regardless of nullability (ADR-0006): NULL FK rows drop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_join_drops_null_fk_rows(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        # One note with an account, one without (nullable FK left NULL).
        await QJNote(id=1, body="attached", account=core["a1"]).save()
        await QJNote(id=2, body="orphan", account=None).save()

        rows = await QJNote.where(lambda n: n.account.ledger_id == 1).all()
        assert {r.id for r in rows} == {1}
        # count() agrees with the INNER-narrowed result set.
        count = await QJNote.where(lambda n: n.account.ledger_id == 1).count()
        assert count == 1


# ---------------------------------------------------------------------------
# Join dedup: same path twice / within one &/| tree -> one join (behavioral).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_dedup_across_where_calls_and_boolean_tree(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        # Same relation path referenced in two where() calls.
        q_two_calls = QJTransaction.where(
            lambda t: t.account.ledger_id == 1
        ).where(lambda t: t.account.owner.email == "o1@ferro.dev")
        assert list(q_two_calls._joins) == [("account",), ("account", "owner")]
        rows = await q_two_calls.all()
        # Ledger A + owner o1 -> account a1 -> transactions 1,2,3.
        assert {r.id for r in rows} == {1, 2, 3}

        # Same path twice within one & tree registers once.
        q_and = QJTransaction.where(
            lambda t: (t.account.ledger_id == 1) & (t.account.label == "a1")
        )
        assert list(q_and._joins) == [("account",)]
        rows_and = await q_and.all()
        assert {r.id for r in rows_and} == {1, 2, 3}


@pytest.mark.asyncio
async def test_or_composition_still_renders_inner_join_narrowing_query_wide(db_url):
    """ADR-0006 narrowing is query-wide: a traversal branch inside an ``|`` still
    renders the INNER join, so a relation-less row is dropped even when the OTHER
    branch (a root-column predicate) would have matched it."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await QJNote(id=1, body="attached", account=core["a1"]).save()
        await QJNote(id=2, body="orphan", account=None).save()

        rows = await QJNote.where(
            lambda n: (n.account.ledger_id == 1) | (n.body == "orphan")
        ).all()
        # The INNER account join drops the NULL-FK orphan BEFORE the WHERE, so it
        # never reaches the `n.body == "orphan"` branch — only the attached note
        # (ledger 1) survives.
        assert {r.id for r in rows} == {1}
        count = await QJNote.where(
            lambda n: (n.account.ledger_id == 1) | (n.body == "orphan")
        ).count()
        assert count == 1


# ---------------------------------------------------------------------------
# Two FKs to one target (transfer pair) — distinct joins, no alias ceremony.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_fks_to_one_target(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        # a1 -> ledger A, b1 -> ledger B.
        await QJTransfer(id=1, from_account=core["a1"], to_account=core["b1"]).save()
        await QJTransfer(id=2, from_account=core["b1"], to_account=core["a1"]).save()

        out_of_a = await QJTransfer.where(
            lambda tr: tr.from_account.ledger_id == 1
        ).all()
        assert {r.id for r in out_of_a} == {1}

        into_a = await QJTransfer.where(lambda tr: tr.to_account.ledger_id == 1).all()
        assert {r.id for r in into_a} == {2}


# ---------------------------------------------------------------------------
# Self-FK: Employee.manager traversal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_fk_traversal(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        boss = QJEmployee(id=1, name="boss", manager=None)
        await boss.save()
        alice = QJEmployee(id=2, name="alice", manager=boss)
        await alice.save()
        await QJEmployee(id=3, name="bob", manager=boss).save()
        await QJEmployee(id=4, name="carol", manager=alice).save()

        managed_by_boss = await QJEmployee.where(
            lambda e: e.manager.name == "boss"
        ).all()
        assert {e.name for e in managed_by_boss} == {"alice", "bob"}


# ---------------------------------------------------------------------------
# Verb composition + count() equals the matching root-row count.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbs_compose_and_count_is_root_row_count(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        base = QJTransaction.where(lambda t: t.account.ledger_id == 1)

        # Ledger A has 4 transactions (fan-in: 3 on a1, 1 on a2). A naive
        # row-multiplying join would over-count; INNER many-to-one does not.
        assert await base.count() == 4
        assert await base.exists() is True

        all_rows = await base.order_by(lambda t: t.id).all()
        assert [r.id for r in all_rows] == [1, 2, 3, 4]

        first = await base.order_by(lambda t: t.id).first()
        assert first is not None and first.id == 1

        page = await base.order_by(lambda t: t.id).limit(2).offset(1).all()
        assert [r.id for r in page] == [2, 3]

        empty = QJTransaction.where(lambda t: t.account.owner.email == "nobody@x")
        assert await empty.count() == 0
        assert await empty.exists() is False


# ---------------------------------------------------------------------------
# Immutability: branching a joined query never mutates the base.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_immutability_with_joins(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        base = QJTransaction.where(lambda t: t.amount >= 30)
        branched = base.where(lambda t: t.account.ledger_id == 2)

        # _joins isolation: the base never acquires the branch's join.
        assert base._joins == {}
        assert list(branched._joins) == [("account",)]
        assert base._joins is not branched._joins

        base_rows = await base.all()
        # Base is a plain root-column filter: amount >= 30 -> ids 3,4,5,6.
        assert {r.id for r in base_rows} == {3, 4, 5, 6}

        branched_rows = await branched.all()
        # amount >= 30 AND ledger B (b1: 50,60) -> ids 5,6.
        assert {r.id for r in branched_rows} == {5, 6}


# ---------------------------------------------------------------------------
# order_by() relation traversal (#271): shares joins with where(), any depth,
# asc/desc, INNER-drops-NULL, and a same-path dedup with where().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_by_related_column_asc_and_desc(db_url):
    """1-hop `order_by(lambda t: t.account.label)`, both directions, with a
    root-column tiebreaker so ties can never make the assertion flaky."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        asc_rows = await (
            QJTransaction.select()
            .order_by(lambda t: t.account.label)
            .order_by(lambda t: t.id)
            .all()
        )
        # Labels ascending a1 < a2 < b1; ties broken by id.
        assert [r.id for r in asc_rows] == [1, 2, 3, 4, 5, 6]

        desc_rows = await (
            QJTransaction.select()
            .order_by(lambda t: t.account.label, "desc")
            .order_by(lambda t: t.id)
            .all()
        )
        # Labels descending b1 > a2 > a1; ties broken by id ascending within
        # each label group — proves the sort key is the related column, not id.
        assert [r.id for r in desc_rows] == [5, 6, 4, 1, 2, 3]


@pytest.mark.asyncio
async def test_order_by_multi_hop_shares_join_with_where_same_path(db_url):
    """`order_by(lambda t: t.account.owner.email)` composed with a `where()`
    traversal of the SAME path -> exactly one join entry, and correct rows."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        q = (
            QJTransaction.where(lambda t: t.account.owner.email == "o1@ferro.dev")
            .order_by(lambda t: t.account.owner.email)
            .order_by(lambda t: t.id)
        )
        # Behavioral half of the one-join acceptance criterion.
        assert list(q._joins) == [("account", "owner")]

        rows = await q.all()
        # Owner o1 owns a1 (txns 1,2,3) and b1 (txns 5,6); ordered by email
        # (constant "o1@ferro.dev" within this filtered set) then id.
        assert [r.id for r in rows] == [1, 2, 3, 5, 6]


@pytest.mark.asyncio
async def test_order_by_related_column_composed_with_where_different_path(db_url):
    """`where()` traversing one path and `order_by()` traversing a DIFFERENT
    path -> two distinct join entries, both effective on the result."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_core()

        q = (
            QJTransaction.where(lambda t: t.account.ledger_id == 1)
            .order_by(lambda t: t.account.owner.email)
            .order_by(lambda t: t.id)
        )
        assert list(q._joins) == [("account",), ("account", "owner")]

        rows = await q.all()
        # Ledger A -> accounts a1 (owner o1, txns 1,2,3), a2 (owner o2, txn 4).
        # Owner email ascending (o1 < o2) with id tiebreaker.
        assert [r.id for r in rows] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_order_by_related_column_drops_null_fk_rows(db_url):
    """INNER ordering drops relation-less rows (nullable FK left NULL) — the
    same ADR-0006 semantics `where()` traversal already has, now for order_by."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await QJNote(id=1, body="attached", account=core["a1"]).save()
        await QJNote(id=2, body="orphan", account=None).save()

        rows = await QJNote.select().order_by(lambda n: n.account.label).all()
        assert [r.id for r in rows] == [1]


def test_order_by_bare_relation_proxy_is_rejected():
    """A bare relation (no column selected) is meaningless as a sort key —
    rejected at build time with a pointed message naming the relation (#273)."""
    with pytest.raises(
        TypeError, match="order_by.*bare relation 'account'.*t.account.<column>"
    ):
        Query(QJTransaction).order_by(lambda t: t.account)  # type: ignore[arg-type,return-value]


def test_bare_relation_proxy_predicate_is_rejected():
    """A where() lambda returning a bare relation is not a QueryNode — rejected
    at build time with a pointed message naming the relation (#273)."""
    with pytest.raises(
        TypeError, match="bare relation 'account'.*== None / == an instance"
    ):
        Query(QJTransaction).where(lambda t: t.account)  # type: ignore[arg-type,return-value]


# ---------------------------------------------------------------------------
# Relation-proxy sugar and guardrails (#273): build-time desugaring, error
# taxonomy, boolean-coercion trap, and update()/delete() traversal rejection.
# These are build-time-only (no DB round-trip).
# ---------------------------------------------------------------------------


def _persisted_account(pk: int = 7) -> QJAccount:
    return QJAccount(id=pk, label="x", ledger=QJLedger(id=1), owner=QJOwner(id=1))


class TestRelationProxySugar:
    def _proxy(self) -> QueryProxy:
        return QueryProxy(QJTransaction)

    def test_instance_equality_desugars_to_shadow_fk_join_free(self):
        node = self._proxy().account == _persisted_account(7)
        assert isinstance(node, QueryNode)
        assert node.column == "account_id"
        assert node.operator == "=="
        assert node.value == 7
        assert node.path == ()  # genuinely join-free (path minus last hop)

    def test_instance_inequality_operator(self):
        node = self._proxy().account != _persisted_account(7)
        assert node.operator == "!="
        assert node.column == "account_id"
        assert node.value == 7
        assert node.path == ()

    def test_deep_instance_equality_uses_last_hop_shadow_and_prefix_path(self):
        owner = QJOwner(id=3, email="o@ferro.dev")
        node = self._proxy().account.owner == owner
        # Shadow column of the LAST hop (owner_id, on the account table), with
        # the proxy path MINUS that hop -> the ("account",) prefix join only.
        assert node.column == "owner_id"
        assert node.value == 3
        assert node.path == ("account",)

    def test_equals_none_builds_null_leaf_join_free(self):
        node = self._proxy().account == None  # noqa: E711
        assert node.column == "account_id"
        assert node.value is None
        assert node.operator == "=="
        assert node.path == ()

    def test_not_equals_none_operator(self):
        node = self._proxy().account != None  # noqa: E711
        assert node.operator == "!="
        assert node.value is None
        assert node.path == ()

    def test_unpersisted_instance_raises_value_error_naming_model(self):
        unsaved = QJAccount(label="x", ledger=QJLedger(id=1), owner=QJOwner(id=1))
        with pytest.raises(
            ValueError, match="unpersisted QJAccount instance .primary key not set"
        ):
            self._proxy().account == unsaved

    def test_wrong_model_instance_raises_type_error(self):
        with pytest.raises(
            TypeError, match="relation 'account'.*expected a QJAccount instance or None"
        ):
            self._proxy().account == QJLedger(id=1)

    def test_scalar_comparison_raises_type_error(self):
        with pytest.raises(
            TypeError, match="relation 'account'.*expected a QJAccount instance or None"
        ):
            self._proxy().account == 5

    def test_non_equality_operators_are_rejected(self):
        proxy = self._proxy()
        for build in (
            lambda: proxy.account < 5,
            lambda: proxy.account <= 5,
            lambda: proxy.account > 5,
            lambda: proxy.account >= 5,
            lambda: proxy.account.in_([1, 2]),
            lambda: proxy.account.like("x"),
            lambda: proxy.account << [1, 2],
        ):
            with pytest.raises(TypeError, match="supports only == / !="):
                build()


def test_query_node_boolean_coercion_raises():
    """`and`/`or` misuse coerces a QueryNode to bool; that raises pointedly."""
    node = FieldProxy("age") >= 18
    with pytest.raises(TypeError, match="boolean context.*use & / |"):
        bool(node)
    with pytest.raises(TypeError, match="use & / |"):
        _ = (FieldProxy("age") >= 18) and (FieldProxy("age") <= 30)


class TestMutatingTraversalRejection:
    """update()/delete() reject relation traversal at the shared choke point
    (:func:`ferro.query.wire.compile_query`), before any serialization or DB."""

    def test_traversed_predicate_rejected_for_update_and_delete(self):
        q = Query(QJTransaction).where(lambda t: t.account.label == "a1")
        for operation in ("update", "delete"):
            with pytest.raises(ValueError, match="does not support relation traversal"):
                compile_query(q, operation)

    def test_explicit_join_rejected(self):
        q = Query(QJTransaction).join(lambda t: t.account)
        with pytest.raises(ValueError, match="relation traversal"):
            compile_query(q, "update")

    def test_join_free_relation_filter_is_allowed(self):
        q = Query(QJTransaction).where(lambda t: t.account == _persisted_account(1))
        payload = compile_query(q, "update").to_ir_dict()  # must NOT raise
        assert payload["where"][0]["column"] == "account_id"
        assert payload["where"][0]["path"] == []


@pytest.mark.asyncio
async def test_update_and_delete_reject_traversal_before_db():
    """The rejection fires before any DB round-trip — no connection is made."""
    q = Query(QJTransaction).where(lambda t: t.account.owner.email == "x@y.z")
    with pytest.raises(ValueError, match="relation traversal"):
        await q.update(amount=0)
    with pytest.raises(ValueError, match="relation traversal"):
        await q.delete()


# ---------------------------------------------------------------------------
# Explicit join()/left_join() chainers (#272): state model, conflict rules,
# the pinned mixed LEFT-prefix/INNER-suffix serialization, and immutability.
# These are build-time-only (no DB round-trip).
# ---------------------------------------------------------------------------


def _serialized_join_types(query) -> list[tuple[tuple[str, ...], str]]:
    """(path, join_type) for each serialized ``joins`` entry, in wire order."""
    payload = compile_query(query, "fetch").to_ir_dict()
    return [
        (tuple(hop["relation"] for hop in entry["path"]), entry["join_type"])
        for entry in payload["joins"]
    ]


class TestExplicitJoinChainers:
    def test_join_marks_edges_inner_and_registers_path(self):
        q = Query(QJTransaction).join(lambda t: t.account)
        assert q._explicit_edges == {("account",): "inner"}
        assert list(q._joins) == [("account",)]
        assert _serialized_join_types(q) == [(("account",), "inner")]

    def test_left_join_marks_whole_path_left(self):
        q = Query(QJTransaction).left_join(lambda t: t.account.owner)
        # Whole-path rule: BOTH edges are LEFT-marked (ADR-0006).
        assert q._explicit_edges == {
            ("account",): "left",
            ("account", "owner"): "left",
        }
        assert list(q._joins) == [("account", "owner")]
        assert _serialized_join_types(q) == [(("account", "owner"), "left")]

    def test_left_join_prefix_inner_suffix_serializes_mixed_edges(self):
        """The pinned mixed case: ``left_join(t.account)`` + a deeper INNER
        traversal ``where(t.account.owner.email == x)`` → wire entries
        ``["account"]→left`` and ``["account","owner"]→inner`` (#272)."""
        q = (
            Query(QJTransaction)
            .left_join(lambda t: t.account)
            .where(lambda t: t.account.owner.email == "o1@ferro.dev")
        )
        assert q._explicit_edges == {("account",): "left"}
        assert list(q._joins) == [("account",), ("account", "owner")]
        assert _serialized_join_types(q) == [
            (("account",), "left"),
            (("account", "owner"), "inner"),
        ]

    def test_explicit_left_beats_implicit_inner_same_path(self):
        """A where()-registered INNER path re-marked LEFT by left_join renders
        LEFT — regardless of chainer order (#272)."""
        after = (
            Query(QJTransaction)
            .where(lambda t: t.account.label == "a1")
            .left_join(lambda t: t.account)
        )
        assert _serialized_join_types(after) == [(("account",), "left")]

        before = (
            Query(QJTransaction)
            .left_join(lambda t: t.account)
            .where(lambda t: t.account.label == "a1")
        )
        assert _serialized_join_types(before) == [(("account",), "left")]

    def test_contradictory_join_types_same_edge_raise(self):
        with pytest.raises(ValueError, match="conflicting explicit join"):
            Query(QJTransaction).join(lambda t: t.account).left_join(
                lambda t: t.account
            )
        with pytest.raises(ValueError, match="conflicting explicit join"):
            Query(QJTransaction).left_join(lambda t: t.account).join(
                lambda t: t.account
            )

    def test_contradictory_join_types_overlapping_paths_raise(self):
        """``.join(t.account)`` + ``.left_join(t.account.owner)`` conflict on the
        shared ``account`` edge (whole-path LEFT marks it) — build-time error."""
        with pytest.raises(ValueError, match="account"):
            Query(QJTransaction).join(lambda t: t.account).left_join(
                lambda t: t.account.owner
            )

    def test_contradictory_join_types_overlapping_paths_raise_reverse_order(self):
        """Mirror of the overlap conflict: ``.left_join(t.account.owner)`` THEN
        ``.join(t.account)`` conflicts on the same shared ``account`` edge — the
        whole-path LEFT mark and the later INNER mark disagree regardless of which
        chainer ran first (#272)."""
        with pytest.raises(ValueError, match="account"):
            Query(QJTransaction).left_join(lambda t: t.account.owner).join(
                lambda t: t.account
            )

    def test_remarking_same_direction_is_idempotent(self):
        q = Query(QJTransaction).join(lambda t: t.account).join(lambda t: t.account)
        assert q._explicit_edges == {("account",): "inner"}
        assert list(q._joins) == [("account",)]

    def test_join_selector_rejecting_column_and_non_relation(self):
        with pytest.raises(TypeError, match="not a relation"):
            Query(QJTransaction).join(lambda t: t.account.label)  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError, match="must return a relation path"):
            Query(QJTransaction).left_join(lambda t: 5)  # type: ignore[arg-type,return-value]

    def test_chainers_are_immutable(self):
        base = Query(QJTransaction).where(lambda t: t.amount >= 10)
        branched = base.left_join(lambda t: t.account)
        assert base._joins == {}
        assert base._explicit_edges == {}
        assert list(branched._joins) == [("account",)]
        assert branched._explicit_edges == {("account",): "left"}
        assert base._explicit_edges is not branched._explicit_edges


# ---------------------------------------------------------------------------
# Explicit join()/left_join() behavior against real backends (#272): existence
# filtering, NULL retention in ordered results and traversal predicates, and
# whole-path LEFT retaining rows missing the relation at either hop.
# ---------------------------------------------------------------------------


async def _seed_notes(core):
    """One note attached to a1, one orphan (nullable FK left NULL)."""
    await QJNote(id=1, body="attached", account=core["a1"]).save()
    await QJNote(id=2, body="orphan", account=None).save()


@pytest.mark.asyncio
async def test_bare_join_is_existence_filter(db_url):
    """``.join(lambda n: n.account)`` with no predicate narrows to rows where
    the nullable relation exists; count() agrees."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await _seed_notes(core)

        rows = await QJNote.select().join(lambda n: n.account).all()
        assert {r.id for r in rows} == {1}
        count = await QJNote.select().join(lambda n: n.account).count()
        assert count == 1


@pytest.mark.asyncio
async def test_left_join_retains_relation_less_rows(db_url):
    """A bare ``.left_join`` retains the orphan row (NULL-FK), unlike ``.join``."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await _seed_notes(core)

        rows = await QJNote.select().left_join(lambda n: n.account).all()
        assert {r.id for r in rows} == {1, 2}
        assert await QJNote.select().left_join(lambda n: n.account).count() == 2


@pytest.mark.asyncio
async def test_left_join_null_retention_in_ordered_results(db_url):
    """left_join + order_by on a RELATED column retains the NULL-FK row. NULL
    placement diverges by dialect (ADR-0006: Postgres NULLs last on ASC, SQLite
    first), so assert the full row set + the non-NULL order per-backend."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        # Two attached notes (a1 "a1", a2 "a2") + one orphan → non-NULL order a1,a2.
        await QJNote(id=1, body="attached-a1", account=core["a1"]).save()
        await QJNote(id=2, body="attached-a2", account=core["a2"]).save()
        await QJNote(id=3, body="orphan", account=None).save()

        rows = await (
            QJNote.select()
            .left_join(lambda n: n.account)
            .order_by(lambda n: n.account.label)
            .all()
        )
        ids = [r.id for r in rows]
        # Full set retained (orphan kept by LEFT join).
        assert set(ids) == {1, 2, 3}
        # Non-NULL rows keep their relative order (label a1 < a2 → id 1 before 2).
        assert ids.index(1) < ids.index(2)
        # NULL-FK row's position is dialect-specific but deterministic.
        if db_url.startswith("postgres"):
            assert ids == [1, 2, 3]  # NULLs last on ASC
        else:
            assert ids == [3, 1, 2]  # SQLite sorts NULLs first


@pytest.mark.asyncio
async def test_left_join_null_retention_in_traversal_predicate(db_url):
    """left_join + ``where(n.account.name == None)`` returns exactly the
    relation-less rows (IS NULL on the joined alias column)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await _seed_notes(core)

        rows = await (
            QJNote.select()
            .left_join(lambda n: n.account)
            .where(lambda n: n.account.label == None)  # noqa: E711
            .all()
        )
        assert {r.id for r in rows} == {2}


async def _seed_docs():
    """Three docs spanning every hole a 2-hop path can have.

    Doc 1 -> folder f1 -> owner ow (relation present at both hops);
    doc 2 -> no folder (missing at hop 1);
    doc 3 -> folder f2 whose owner FK is NULL (missing at hop 2).
    """
    ow = QJFolderOwner(id=1, name="ow")
    await ow.save()
    f1 = QJFolder(id=1, label="owned", owner=ow)
    f2 = QJFolder(id=2, label="ownerless", owner=None)
    await f1.save()
    await f2.save()
    await QJDoc(id=1, title="both-hops", folder=f1).save()
    await QJDoc(id=2, title="no-folder", folder=None).save()
    await QJDoc(id=3, title="folder-no-owner", folder=f2).save()


@pytest.mark.asyncio
async def test_whole_path_left_retains_rows_missing_at_either_hop(db_url):
    """A left-marked 2-hop path retains rows missing the relation at hop 1 AND
    rows missing at hop 2 (whole-path LEFT, ADR-0006)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_docs()

        rows = await QJDoc.select().left_join(lambda d: d.folder.owner).all()
        # Whole-path LEFT: both edges LEFT → the hop-1-missing doc (no folder)
        # AND the hop-2-missing doc (folder without owner) are retained.
        assert {r.id for r in rows} == {1, 2, 3}
        count = await QJDoc.select().left_join(lambda d: d.folder.owner).count()
        assert count == 3


@pytest.mark.asyncio
async def test_whole_path_left_traversal_predicate_matches_either_hop_holes(db_url):
    """left_join(2-hop) + ``where(d.folder.owner.name == None)`` returns exactly
    the rows missing the relation at either hop (IS NULL on the hop-2 alias)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_docs()

        rows = await (
            QJDoc.select()
            .left_join(lambda d: d.folder.owner)
            .where(lambda d: d.folder.owner.name == None)  # noqa: E711
            .all()
        )
        # Missing at hop 1 (doc 2) and missing at hop 2 (doc 3); doc 1 resolves
        # a non-NULL owner name and is excluded.
        assert {r.id for r in rows} == {2, 3}


@pytest.mark.asyncio
async def test_explicit_left_plus_implicit_traversal_renders_left(db_url):
    """Explicit ``.left_join`` on a path also traversed implicitly by ``where``
    renders LEFT — the IS NULL predicate keeps the NULL-FK row (#272)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await _seed_notes(core)

        # where() alone would render INNER and drop the orphan; left_join lifts
        # the shared edge to LEFT so the IS NULL match survives.
        rows = await (
            QJNote.select()
            .where(lambda n: n.account.label == None)  # noqa: E711
            .left_join(lambda n: n.account)
            .all()
        )
        assert {r.id for r in rows} == {2}


# ---------------------------------------------------------------------------
# Relation-proxy equality sugar — behavioral (#273): instance-eq filters by the
# shadow FK join-free; == None / != None lower to IS NULL / IS NOT NULL; the
# M2M association context composes with forward-FK traversal on the target.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instance_equality_filters_by_shadow_fk(db_url):
    """`t.account == a1` filters by `account_id` join-free; `!=` complements."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        a1 = core["a1"]

        eq_rows = await QJTransaction.where(lambda t: t.account == a1).all()
        assert {r.id for r in eq_rows} == {1, 2, 3}
        assert await QJTransaction.where(lambda t: t.account == a1).count() == 3

        ne_rows = await QJTransaction.where(lambda t: t.account != a1).all()
        assert {r.id for r in ne_rows} == {4, 5, 6}


@pytest.mark.asyncio
async def test_deep_instance_equality_traverses_prefix_join(db_url):
    """`t.account.owner == o2` compares `owner_id` under the account prefix join
    (the shadow column lives one hop up, not the final target's PK column)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        o2 = core["o2"]

        rows = await QJTransaction.where(lambda t: t.account.owner == o2).all()
        # Only account a2 has owner o2; a2 has one transaction (id=4).
        assert {r.id for r in rows} == {4}


@pytest.mark.asyncio
async def test_relation_equals_none_renders_is_null(db_url):
    """`n.account == None` / `!= None` lower to IS NULL / IS NOT NULL on the
    shadow FK, join-free."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        core = await _seed_core()
        await _seed_notes(core)

        null_rows = await QJNote.where(lambda n: n.account == None).all()  # noqa: E711
        assert {r.id for r in null_rows} == {2}

        present_rows = await QJNote.where(
            lambda n: n.account != None  # noqa: E711
        ).all()
        assert {r.id for r in present_rows} == {1}


@pytest.mark.asyncio
async def test_m2m_context_composes_with_forward_fk_traversal(db_url):
    """An M2M association query (`post.tags`) whose where() traverses a forward
    FK on the target model composes: the association join and the traversal join
    coexist in one statement (#273)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        admin = await QJMAuthor.create(id=1, role="admin")
        member = await QJMAuthor.create(id=2, role="member")
        tag_admin = await QJMTag.create(id=1, name="urgent", created_by=admin)
        tag_member = await QJMTag.create(id=2, name="chill", created_by=member)
        post = await QJMPost.create(id=1, title="p1")
        await post.tags.add(tag_admin, tag_member)

        # Sanity: the unfiltered association returns both tags.
        assert {t.id for t in await post.tags.all()} == {1, 2}

        admin_tags = await post.tags.where(lambda t: t.created_by.role == "admin").all()
        assert {t.id for t in admin_tags} == {1}
        assert (
            await post.tags.where(lambda t: t.created_by.role == "admin").count() == 1
        )


# ---------------------------------------------------------------------------
# Typed binds on TRAVERSED columns (#270 critical): a filter on a related
# datetime / uuid / decimal / native-enum column must bind against the traversed
# hop's model, not the root's codec plan / enum catalog. Before the fix these
# resolved every leaf against the root model and failed on Postgres (e.g.
# `operator does not exist: timestamp with time zone >= text`).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traversed_datetime_filter_binds_against_hop_model(db_url):
    """`u.profile.created_at >= dt` casts the bind to the hop table's temporal
    type — the RED case: on Postgres the root-keyed bug sent an un-cast `text`."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        rows = await QJTBUser.where(
            lambda u: u.profile.created_at >= datetime(2025, 1, 1, tzinfo=timezone.utc)
        ).all()
        # Only P2 (created_at=T2, 2030) is at/after 2025 -> its user U2.
        assert {r.id for r in rows} == {2}
        assert (
            await QJTBUser.where(
                lambda u: u.profile.created_at
                >= datetime(2025, 1, 1, tzinfo=timezone.utc)
            ).count()
            == 1
        )


@pytest.mark.asyncio
async def test_traversed_uuid_filter_binds_against_hop_model(db_url):
    """`u.profile.external_id == uuid` sends a typed uuid bind for the hop table
    (root-keyed bug: `operator does not exist: uuid = text`)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        rows = await QJTBUser.where(
            lambda u: u.profile.external_id == _TB_UID1
        ).all()
        assert {r.id for r in rows} == {1}


@pytest.mark.asyncio
async def test_traversed_decimal_filter_binds_against_hop_model(db_url):
    """`u.profile.balance >= Decimal(...)` casts the bind to numeric for the hop
    table (root-keyed bug: `operator does not exist: numeric >= text`)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        rows = await QJTBUser.where(
            lambda u: u.profile.balance >= Decimal("50.00")
        ).all()
        # Only P1 (balance=100) clears 50 -> user U1.
        assert {r.id for r in rows} == {1}


@pytest.mark.asyncio
async def test_traversed_native_enum_filter_binds_against_hop_model(db_url):
    """`u.profile.tier == QJTBTier.PRO` casts to the hop table's native enum UDT
    (root-keyed bug: no UDT cast -> `operator does not exist: qjtbtier = text`)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        rows = await QJTBUser.where(lambda u: u.profile.tier == QJTBTier.PRO).all()
        assert {r.id for r in rows} == {1}


@pytest.mark.asyncio
async def test_traversed_typed_null_filter_binds_against_hop_model(db_url):
    """`u.profile.nickname == None` lowers to IS NULL on the hop alias column and
    returns exactly the rows whose related nickname is NULL."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        rows = await QJTBUser.where(
            lambda u: u.profile.nickname == None  # noqa: E711
        ).all()
        # Only P2 has nickname NULL -> user U2.
        assert {r.id for r in rows} == {2}


@pytest.mark.asyncio
async def test_name_collision_root_and_traversed_column_bind_independently(db_url):
    """`tag` names a NATIVE ENUM on the root (QJTBUser) and PLAIN TEXT on the
    related (QJTBProfile). Both must bind against their OWN table: the root-keyed
    bug applied the root's enum-UDT cast to the traversed text column
    (`invalid input value for enum qjtbtier: "vip"`)."""
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _seed_typed_bind_matrix()

        # Root enum column: filters by the native-enum bind.
        root_rows = await QJTBUser.where(lambda u: u.tag == QJTBTier.PRO).all()
        assert {r.id for r in root_rows} == {1}

        # Traversed text column of the SAME name: plain-text bind, no enum cast.
        traversed_rows = await QJTBUser.where(lambda u: u.profile.tag == "vip").all()
        assert {r.id for r in traversed_rows} == {1}

        # A value that is NOT a valid enum label must simply not match (never
        # blow up as an enum cast against the text column).
        empty = await QJTBUser.where(lambda u: u.profile.tag == "nope").all()
        assert empty == []
