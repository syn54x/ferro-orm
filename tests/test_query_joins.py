"""Backend-matrix integration tests for relation traversal in where() (#270).

Declares FK-related models, inserts rows, and asserts result sets against real
SQLite and isolated-schema Postgres backends — no SQL-string snapshots at this
layer (the rendered-SQL pins live in the Rust walker unit tests). Covers the
motivating Pinch query, multi-hop traversal, per-hop did-you-mean, INNER-drops-
NULL semantics (ADR-0006), join dedup, two-FKs-to-one-target, self-FK, verb
composition, and query immutability.
"""

from typing import Annotated

import pytest

import ferro
from ferro import BackRef, FerroField, ForeignKey, Model, Relation
from ferro.query import Query

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


def test_bare_relation_proxy_predicate_is_rejected():
    """A where() lambda returning a bare relation (no comparison) is not a
    QueryNode — rejected at build time (comparison sugar is slice #273)."""
    with pytest.raises(TypeError, match="must return QueryNode"):
        Query(QJTransaction).where(lambda t: t.account)  # type: ignore[arg-type,return-value]


def test_order_by_relation_traversal_is_rejected():
    """order_by relation traversal is slice #271 — a clear build-time error now."""
    with pytest.raises(TypeError, match="relation traversal"):
        Query(QJTransaction).order_by(lambda t: t.account.owner.email)
