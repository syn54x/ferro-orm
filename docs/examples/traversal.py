"""Runnable companion to the "Querying Across Relationships" guide section
(docs/pages/guide/queries.md).

Seeds a small ledger domain (Ledger <- Account -> Owner, with Transactions and
Notes hanging off Account) plus self-contained schemas for the two-FK, self-FK,
and many-to-many patterns, then runs every traversal query the guide shows and
asserts the exact rows that come back.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect, engines


# --8<-- [start:schema]
class Ledger(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    accounts: Relation[list["Account"]] = BackRef()


class Owner(Model):
    id: int | None = Field(default=None, primary_key=True)
    email: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    ledger: Annotated[Ledger, ForeignKey(related_name="accounts")]
    owner: Annotated[Owner, ForeignKey(related_name="accounts")]
    transactions: Relation[list["Transaction"]] = BackRef()
    notes: Relation[list["Note"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount: int
    account: Annotated[Account, ForeignKey(related_name="transactions")]
# --8<-- [end:schema]


# --8<-- [start:note-model]
class Note(Model):
    id: int | None = Field(default=None, primary_key=True)
    body: str
    account: Annotated[Account | None, ForeignKey(related_name="notes")] = None
# --8<-- [end:note-model]


# --8<-- [start:two-fk-model]
class Airport(Model):
    id: int | None = Field(default=None, primary_key=True)
    code: str
    departures: Relation[list["Flight"]] = BackRef()
    arrivals: Relation[list["Flight"]] = BackRef()


class Flight(Model):
    id: int | None = Field(default=None, primary_key=True)
    origin: Annotated[Airport, ForeignKey(related_name="departures")]
    destination: Annotated[Airport, ForeignKey(related_name="arrivals")]
# --8<-- [end:two-fk-model]


# --8<-- [start:self-fk-model]
class Employee(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    manager: Annotated["Employee", ForeignKey(related_name="reports", nullable=True)] = None
    reports: Relation[list["Employee"]] = BackRef()
# --8<-- [end:self-fk-model]


# --8<-- [start:m2m-model]
class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    role: str
    tags: Relation[list["Tag"]] = BackRef()


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    created_by: Annotated[Author, ForeignKey(related_name="tags")]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    tags: Relation[list["Tag"]] = ManyToMany(related_name="posts")
# --8<-- [end:m2m-model]


async def _seed_ledger() -> None:
    """Two ledgers, two owners, three accounts, six fan-in transactions.

    Ledger A holds a1 (owner o1) and a2 (owner o2); Ledger B holds b1 (owner o1).
    Transactions: a1 has 10/20/30, a2 has 40, b1 has 50/60.
    """
    la, lb = Ledger(id=1, name="ledger-a"), Ledger(id=2, name="ledger-b")
    o1, o2 = Owner(id=1, email="o1@ferro.dev"), Owner(id=2, email="o2@ferro.dev")
    for row in (la, lb, o1, o2):
        await row.save()

    a1 = Account(id=1, label="a1", ledger=la, owner=o1)
    a2 = Account(id=2, label="a2", ledger=la, owner=o2)
    b1 = Account(id=3, label="b1", ledger=lb, owner=o1)
    for row in (a1, a2, b1):
        await row.save()

    for txn in (
        Transaction(id=1, amount=10, account=a1),
        Transaction(id=2, amount=20, account=a1),
        Transaction(id=3, amount=30, account=a1),
        Transaction(id=4, amount=40, account=a2),
        Transaction(id=5, amount=50, account=b1),
        Transaction(id=6, amount=60, account=b1),
    ):
        await txn.save()

    # One note attached to a1, one orphan (nullable FK left NULL).
    await Note(id=1, body="attached", account=a1).save()
    await Note(id=2, body="orphan", account=None).save()


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await _seed_ledger()

        ledger_a = await Ledger.get(1)
        owner_o2 = await Owner.get(2)
        account_a1 = await Account.get(1)

        # --8<-- [start:basic]
        # Every transaction whose account belongs to ledger A.
        rows = await Transaction.where(
            lambda transaction: transaction.account.ledger_id == ledger_a.id
        ).all()
        # --8<-- [end:basic]
        assert {r.id for r in rows} == {1, 2, 3, 4}

        # --8<-- [start:pinch]
        top = await (
            Transaction.where(lambda transaction: transaction.account.ledger_id == ledger_a.id)
            .where(lambda transaction: transaction.amount >= 20)
            .order_by(lambda transaction: transaction.amount, "desc")
            .limit(2)
            .all()
        )
        # --8<-- [end:pinch]
        assert [r.amount for r in top] == [40, 30]

        # --8<-- [start:multi-hop]
        rows = await Transaction.where(
            lambda transaction: transaction.account.owner.email == "o2@ferro.dev"
        ).all()
        # --8<-- [end:multi-hop]
        assert {r.id for r in rows} == {4}

        # --8<-- [start:inner-narrows]
        # Filtering through note.account drops the orphan note (NULL account FK).
        ledger_a_notes = await Note.where(
            lambda note: note.account.ledger_id == ledger_a.id
        ).all()
        # --8<-- [end:inner-narrows]
        assert {r.id for r in ledger_a_notes} == {1}

        # --8<-- [start:count]
        ledger_a_count = await Transaction.where(
            lambda transaction: transaction.account.ledger_id == ledger_a.id
        ).count()
        # --8<-- [end:count]
        assert ledger_a_count == 4

        # --8<-- [start:order-by]
        ordered = await (
            Transaction.select()
            .order_by(lambda transaction: transaction.account.label)
            .order_by(lambda transaction: transaction.id)
            .all()
        )
        # --8<-- [end:order-by]
        assert [r.id for r in ordered] == [1, 2, 3, 4, 5, 6]

        # --8<-- [start:instance-eq]
        # `== instance` filters by the shadow FK column, with no join.
        on_a1 = await Transaction.where(
            lambda transaction: transaction.account == account_a1
        ).all()
        not_on_a1 = await Transaction.where(
            lambda transaction: transaction.account != account_a1
        ).all()
        # --8<-- [end:instance-eq]
        assert {r.id for r in on_a1} == {1, 2, 3}
        assert {r.id for r in not_on_a1} == {4, 5, 6}

        # --8<-- [start:deep-instance-eq]
        owned_by_o2 = await Transaction.where(
            lambda transaction: transaction.account.owner == owner_o2
        ).all()
        # --8<-- [end:deep-instance-eq]
        assert {r.id for r in owned_by_o2} == {4}

        # --8<-- [start:is-null]
        # Join-free IS NULL / IS NOT NULL on the shadow FK.
        orphans = await Note.where(lambda note: note.account == None).all()  # noqa: E711
        attached = await Note.where(lambda note: note.account != None).all()  # noqa: E711
        # --8<-- [end:is-null]
        assert {r.id for r in orphans} == {2}
        assert {r.id for r in attached} == {1}

        # --8<-- [start:existence]
        # Bare .join() keeps only rows whose nullable relation exists.
        with_account = await Note.select().join(lambda note: note.account).all()
        # --8<-- [end:existence]
        assert {r.id for r in with_account} == {1}

        # --8<-- [start:left-join]
        # .left_join() keeps the orphan row that traversal would drop.
        all_notes = await Note.select().left_join(lambda note: note.account).all()
        # --8<-- [end:left-join]
        assert {r.id for r in all_notes} == {1, 2}

        # --8<-- [start:mutate-limitation]
        # Traversed predicates cannot mutate — fetch primary keys first...
        ids = [
            note.id
            for note in await Note.where(
                lambda note: note.account.ledger_id == ledger_a.id
            ).all()
        ]
        # ...then delete by that key set with a join-free predicate.
        await Note.where(lambda note: note.id.in_(ids)).delete()
        # --8<-- [end:mutate-limitation]
        assert await Note.where(lambda note: note.id.in_(ids)).count() == 0
        # Restore the note for later assertions in this script.
        await Note(id=1, body="attached", account=account_a1).save()

        await _run_two_fk()
        await _run_self_fk()
        await _run_m2m()

    print("traversal example ran successfully")


async def _run_two_fk() -> None:
    jfk = await Airport.create(id=1, code="JFK")
    lax = await Airport.create(id=2, code="LAX")
    await Flight.create(id=1, origin=jfk, destination=lax)
    await Flight.create(id=2, origin=lax, destination=jfk)

    # --8<-- [start:two-fk-query]
    # Each FK is its own relation path, so each traversal is its own join —
    # no aliases to name.
    departing_jfk = await Flight.where(lambda flight: flight.origin.code == "JFK").all()
    arriving_jfk = await Flight.where(
        lambda flight: flight.destination.code == "JFK"
    ).all()
    # --8<-- [end:two-fk-query]
    assert {f.id for f in departing_jfk} == {1}
    assert {f.id for f in arriving_jfk} == {2}


async def _run_self_fk() -> None:
    boss = await Employee.create(id=1, name="boss", manager=None)
    await Employee.create(id=2, name="alice", manager=boss)
    await Employee.create(id=3, name="bob", manager=boss)

    # --8<-- [start:self-fk-query]
    boss_reports = await Employee.where(
        lambda employee: employee.manager.name == "boss"
    ).all()
    # --8<-- [end:self-fk-query]
    assert {e.name for e in boss_reports} == {"alice", "bob"}


async def _run_m2m() -> None:
    admin = await Author.create(id=1, role="admin")
    member = await Author.create(id=2, role="member")
    urgent = await Tag.create(id=1, name="urgent", created_by=admin)
    chill = await Tag.create(id=2, name="chill", created_by=member)
    post = await Post.create(id=1, title="launch")
    await post.tags.add(urgent, chill)

    # --8<-- [start:m2m-query]
    # The association context (post.tags) and forward-FK traversal on the tag
    # compose in one statement.
    admin_tags = await post.tags.where(
        lambda tag: tag.created_by.role == "admin"
    ).all()
    # --8<-- [end:m2m-query]
    assert {t.id for t in admin_tags} == {1}


if __name__ == "__main__":
    asyncio.run(main())
