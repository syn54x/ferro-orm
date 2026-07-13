"""Runnable companion to the "Selecting a Column Subset" guide section
(docs/pages/guide/queries.md).

Seeds a small transactions table, then runs every partial-select query the
guide shows and asserts the exact records that come back.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, Rows, connect, engines


# --8<-- [start:schema]
class Owner(Model):
    id: int | None = Field(default=None, primary_key=True)
    email: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount: int
    memo: str
    account: Annotated[
        Account | None, ForeignKey(related_name="transactions")
    ] = None
# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        alice = await Owner.create(id=1, email="alice@example.com")
        a1 = await Account.create(id=1, label="a1", owner=alice)
        b1 = await Account.create(id=2, label="b1")
        await Transaction.create(id=1, amount=10, memo="coffee", account=a1)
        await Transaction.create(id=2, amount=20, memo="lunch", account=a1)
        await Transaction.create(id=3, amount=30, memo="dinner", account=b1)

        # --8<-- [start:basic]
        rows = await Transaction.select(lambda t: (t.id, t.amount)).all()

        rows[0].amount  # attribute access, like a model
        rows.model_dump()  # [{"id": 1, "amount": 10}, ...]
        # --8<-- [end:basic]
        assert {(r.id, r.amount) for r in rows} == {(1, 10), (2, 20), (3, 30)}
        assert rows[0].model_dump() == {"id": rows[0].id, "amount": rows[0].amount}

        # --8<-- [start:not-a-model]
        row = rows[0]
        assert not isinstance(row, Transaction)  # a Row, not a Transaction
        assert not hasattr(row, "save")  # records cannot be persisted
        # --8<-- [end:not-a-model]

        # --8<-- [start:single]
        amounts = await Transaction.select(lambda t: t.amount).order_by("amount").all()
        assert [r.amount for r in amounts] == [10, 20, 30]
        # --8<-- [end:single]

        # --8<-- [start:container]
        newest = await (
            Transaction.select(lambda t: (t.id, t.memo)).order_by("id", "desc").all()
        )

        assert len(newest) == 3  # list-like: len ...
        assert newest[0].memo == "dinner"  # ... index ...
        top_two = newest[:2]  # ... and slice (a Rows again)
        assert isinstance(top_two, Rows)
        assert top_two.model_dump() == [
            {"id": 3, "memo": "dinner"},
            {"id": 2, "memo": "lunch"},
        ]
        # --8<-- [end:container]

        # --8<-- [start:strings]
        rows = await Transaction.select("id", "amount").order_by("id").all()
        assert rows[0].model_dump() == {"id": 1, "amount": 10}
        # --8<-- [end:strings]

        # --8<-- [start:traversed]
        # A selected field may reach across a relation, at any depth.
        # Unaliased, the field takes the bare leaf column name.
        rows = await (
            Transaction.select(lambda t: (t.memo, t.account.label))
            .order_by("id")
            .all()
        )
        assert rows[0].model_dump() == {"memo": "coffee", "label": "a1"}
        # --8<-- [end:traversed]

        # --8<-- [start:aliases]
        # A dict-returning selector names the output fields — keys are the
        # record's field names, values may traverse.
        rows = await (
            Transaction.select(
                lambda t: {
                    "memo": t.memo,
                    "account_label": t.account.label,
                    "owner_email": t.account.owner.email,
                }
            )
            .order_by("id")
            .all()
        )
        assert rows[0].model_dump() == {
            "memo": "coffee",
            "account_label": "a1",
            "owner_email": "alice@example.com",
        }
        # --8<-- [end:aliases]

        # --8<-- [start:traversal-narrows]
        # Traversal narrows exactly like a where() predicate on the same
        # path: rows without the relation drop out (b1 has no owner)...
        rows = await Transaction.select(
            lambda t: {"memo": t.memo, "owner_email": t.account.owner.email}
        ).all()
        assert {r.memo for r in rows} == {"coffee", "lunch"}

        # ... and left_join() is the opt-out: rows are kept, and their
        # traversed fields decode to None — even from a non-nullable column.
        rows = await (
            Transaction.select(
                lambda t: {"memo": t.memo, "owner_email": t.account.owner.email}
            )
            .left_join(lambda t: t.account.owner)
            .order_by("id")
            .all()
        )
        assert rows.model_dump()[2] == {"memo": "dinner", "owner_email": None}
        # --8<-- [end:traversal-narrows]

        # --8<-- [start:collision]
        # Two selected fields resolving to the same output name is a
        # build-time error naming the fix: the dict form.
        try:
            Transaction.select(lambda t: (t.id, t.account.id))
        except ValueError as exc:
            assert "two fields named 'id'" in str(exc)
            assert "dict selector form" in str(exc)
        # --8<-- [end:collision]

        # --8<-- [start:compose]
        # Projection composes with everything a read query can do: traversal
        # predicates, ordering by unselected columns, limit/offset, first().
        rows = await (
            Transaction.select(lambda t: (t.id, t.memo))
            .where(lambda t: t.account.label == "a1")
            .order_by("amount", "desc")  # amount is not selected — still sorts
            .limit(1)
            .all()
        )
        assert rows.model_dump() == [{"id": 2, "memo": "lunch"}]

        row = await Transaction.select(lambda t: t.memo).order_by("id").first()
        assert row is not None and row.memo == "coffee"
        # --8<-- [end:compose]

        # --8<-- [start:count]
        # count()/exists() are unaffected by projection: they measure the
        # same matching rows a full query would.
        q = Transaction.select(lambda t: (t.id,)).where(lambda t: t.amount >= 20)
        assert await q.count() == 2
        assert await q.exists() is True
        # --8<-- [end:count]

    print("partial_selects example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
