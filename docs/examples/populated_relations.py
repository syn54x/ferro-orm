"""Runnable companion to the "Populating Relations with include()" guide
section (docs/pages/guide/queries.md) and the populated-relations parts of
the relationships guide.

Seeds a small ledger domain (Owner <- Account <- Transaction, both FKs
nullable) and runs every include() query the guides show, asserting the exact
populations that come back — the population contract, membership preservation,
multi-hop paths, identity/accumulation, the refresh rule, and the loud limits.

Two session blocks on purpose: populations accumulate across a session's
queries, so the awaitable-contract and accumulation demos need instances no
earlier include has touched.
"""

import asyncio
import inspect
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines, execute


# --8<-- [start:schema]
class Owner(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount: int
    account: Annotated[Account | None, ForeignKey(related_name="transactions")] = None


# --8<-- [end:schema]


async def _seed() -> None:
    """Owner o1 <- accounts a1/a2; account b1 has no owner; txn 5 has no
    account. Transactions: a1 has 10/20, a2 has 30, b1 has 40, orphan 50."""
    o1 = Owner(id=1, name="o1")
    await o1.save()
    a1 = Account(id=1, label="a1", owner=o1)
    a2 = Account(id=2, label="a2", owner=o1)
    b1 = Account(id=3, label="b1", owner=None)
    for row in (a1, a2, b1):
        await row.save()
    for txn in (
        Transaction(id=1, amount=10, account=a1),
        Transaction(id=2, amount=20, account=a1),
        Transaction(id=3, amount=30, account=a2),
        Transaction(id=4, amount=40, account=b1),
        Transaction(id=5, amount=50, account=None),
    ):
        await txn.save()


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await _seed()

        # --8<-- [start:awaitable]
        # Without include(), a relation keeps the awaitable contract — access
        # is a coroutine that runs its own query.
        transaction = await Transaction.get(2)

        account = await transaction.account
        assert account.label == "a1"
        # --8<-- [end:awaitable]

        # --8<-- [start:basic]
        # One SQL statement; every transaction's account arrives populated.
        transactions = await Transaction.select().include(lambda t: t.account).all()

        for transaction in transactions[:3]:
            # Plain attribute access — no await, no query.
            print(transaction.amount, transaction.account.label)
        # --8<-- [end:basic]
        assert [t.account.label for t in transactions[:3]] == ["a1", "a1", "a2"]

        # --8<-- [start:membership]
        # include() never changes which rows come back: the transaction whose
        # account FK is NULL is still in the result, populated as None.
        every = await Transaction.select().include(lambda t: t.account).all()

        orphan = next(t for t in every if t.id == 5)
        assert len(every) == 5
        assert orphan.account is None
        # --8<-- [end:membership]

        # --8<-- [start:multi-hop]
        # Including a path populates EVERY hop along it.
        transactions = await (
            Transaction.select().include(lambda t: t.account.owner).order_by("id").all()
        )

        first = transactions[0]
        assert first.account.label == "a1"  # hop 1 populated
        assert first.account.owner.name == "o1"  # hop 2 populated

        # A NULL mid-chain ends the chain truthfully: b1 has no owner.
        ownerless = transactions[3]
        assert ownerless.account.label == "b1"
        assert ownerless.account.owner is None
        # --8<-- [end:multi-hop]

        # --8<-- [start:identity]
        # In a session, a populated instance IS the instance — the same object
        # a direct fetch returns, deduped across result rows.
        held = await Account.get(1)
        transactions = await (
            Transaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.amount <= 20)
            .all()
        )

        assert transactions[0].account is held
        assert transactions[0].account is transactions[1].account
        # --8<-- [end:identity]

    async with engines.session():
        # --8<-- [start:accumulate]
        # Populations accumulate across a session's queries: a later query's
        # include attaches onto the instances the session already holds.
        plain = await Transaction.get(3)  # no population yet
        await Transaction.select().include(lambda t: t.account).all()

        assert plain.account.label == "a2"  # now populated
        # --8<-- [end:accumulate]

        # --8<-- [start:count]
        # count()/exists() measure the same rows with or without the include.
        base = Transaction.where(lambda t: t.amount >= 20)
        assert await base.include(lambda t: t.account).count() == await base.count()
        # --8<-- [end:count]

        # --8<-- [start:interplay]
        # A predicate on the same path keeps its stage-1 INNER semantics —
        # include attaches data, it never rewrites what a filter matches.
        a1_rows = await (
            Transaction.where(lambda t: t.account.label == "a1")
            .include(lambda t: t.account)
            .all()
        )
        assert [t.id for t in a1_rows] == [1, 2]
        assert all(t.account.label == "a1" for t in a1_rows)
        # --8<-- [end:interplay]

        # --8<-- [start:refresh-rule]
        # A refresh keeps a population only while it is still true. Change the
        # row's FK underneath the ORM and re-fetch: the stale population is
        # dropped, and access reverts to the awaitable.
        transaction = await (
            Transaction.select()
            .include(lambda t: t.account)
            .where(lambda t: t.id == 1)
            .first()
        )
        assert transaction.account.label == "a1"

        await execute('UPDATE "transaction" SET account_id = ? WHERE id = ?', 2, 1)
        await Transaction.get(1)  # refreshes in place

        pending = transaction.account  # awaitable again
        assert inspect.iscoroutine(pending)
        assert (await pending).label == "a2"
        # --8<-- [end:refresh-rule]

        # --8<-- [start:limits]
        # The loud limits: reverse relations, projections, and mutations.
        try:
            Account.select().include(lambda a: a.transactions)
        except TypeError as exc:
            assert "reverse (BackRef) or many-to-many" in str(exc)

        try:
            Transaction.select().include(lambda t: t.account).select(lambda t: (t.id,))
        except ValueError as exc:
            assert "#282" in str(exc)

        try:
            await Transaction.select().include(lambda t: t.account).delete()
        except ValueError as exc:
            assert "does not support include()" in str(exc)
        # --8<-- [end:limits]

    print("populated_relations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
