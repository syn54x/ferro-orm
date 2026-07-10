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
    rejected at build time (comparison sugar is slice #273's scope)."""
    with pytest.raises(TypeError, match="relation"):
        Query(QJTransaction).order_by(lambda t: t.account)  # type: ignore[arg-type,return-value]


def test_bare_relation_proxy_predicate_is_rejected():
    """A where() lambda returning a bare relation (no comparison) is not a
    QueryNode — rejected at build time (comparison sugar is slice #273)."""
    with pytest.raises(TypeError, match="must return QueryNode"):
        Query(QJTransaction).where(lambda t: t.account)  # type: ignore[arg-type,return-value]


# ---------------------------------------------------------------------------
# Explicit join()/left_join() chainers (#272): state model, conflict rules,
# the pinned mixed LEFT-prefix/INNER-suffix serialization, and immutability.
# These are build-time-only (no DB round-trip).
# ---------------------------------------------------------------------------


def _serialized_join_types(query) -> list[tuple[tuple[str, ...], str]]:
    """(path, join_type) for each serialized ``joins`` entry, in wire order."""
    return [
        (tuple(hop["relation"] for hop in entry["path"]), entry["join_type"])
        for entry in query._serialize_joins()
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
