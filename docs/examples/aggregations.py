"""Runnable companion to the "Aggregations and grouped queries" guide
(docs/pages/guide/aggregations.md).

Seeds a small ledger of four transactions, then runs every aggregate query
the guide shows and asserts the exact records that come back. The
``aggregations`` prelude imports ``Account``, ``Transaction`` and ``seed`` from
here, so the guide's inline blocks run against the same rows.
"""

import asyncio
from decimal import Decimal
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


# --8<-- [start:schema]
class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
    amount: int
    price: Decimal | None = None
    memo: str
    account: Annotated[Account | None, ForeignKey(related_name="transactions")] = None


# --8<-- [end:schema]


async def seed() -> None:
    """Add the guide's rows. Call inside a session."""
    checking = await Account.create(id=1, label="Checking")
    savings = await Account.create(id=2, label="Savings")
    await Transaction.create(
        id=1, amount=10, price=Decimal("1.25"), memo="coffee", account=checking
    )
    await Transaction.create(
        id=2, amount=20, price=Decimal("2.75"), memo="lunch", account=checking
    )
    await Transaction.create(
        id=3, amount=45, price=Decimal("5.00"), memo="dinner", account=savings
    )
    await Transaction.create(id=4, amount=40, memo="orphan")  # no account, no price


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:global]
    async with engines.session():
        await seed()

        # An aggregate-only projection collapses to exactly one record,
        # read idiomatically with first().
        row = await Transaction.select(
            lambda transaction: {
                "n": transaction.id.count(),
                "total": transaction.amount.sum(),
                "average": transaction.amount.avg(),
                "smallest": transaction.amount.min(),
                "largest": transaction.amount.max(),
            }
        ).first()

        assert row is not None
        assert row.model_dump() == {
            "n": 4,
            "total": 115,
            "average": 28.75,
            "smallest": 10,
            "largest": 45,
        }
        # --8<-- [end:global]

        # --8<-- [start:global-where]
        # Aggregates measure whatever where() leaves in, traversal included.
        row = await (
            Transaction.select(lambda transaction: {"total": transaction.amount.sum()})
            .where(lambda transaction: transaction.account.label == "Checking")
            .first()
        )
        assert row is not None and row.total == 30
        # --8<-- [end:global-where]

        # --8<-- [start:empty]
        # Over zero matching rows: one record, None for sum, 0 for count.
        row = await (
            Transaction.select(
                lambda transaction: {
                    "n": transaction.id.count(),
                    "total": transaction.amount.sum(),
                }
            )
            .where(lambda transaction: transaction.amount > 10_000)
            .first()
        )
        assert row is not None
        assert row.n == 0
        assert row.total is None
        # --8<-- [end:empty]

        # --8<-- [start:count-nulls]
        # COUNT(column) counts the non-NULL values of that column. Three of
        # the four transactions have a price; the orphan has none.
        row = await Transaction.select(
            lambda transaction: {
                "rows": transaction.id.count(),
                "priced": transaction.price.count(),
            }
        ).first()
        assert row is not None
        assert row.rows == 4
        assert row.priced == 3
        # --8<-- [end:count-nulls]

        # --8<-- [start:grouped]
        # Mix a plain field in and the query becomes grouped: every
        # non-aggregate field is a group key. There is no group_by().
        rows = await (
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            )
            .where(lambda transaction: transaction.account_id != None)  # noqa: E711
            .order_by("acct")
            .all()
        )
        assert rows.model_dump() == [
            {"acct": 1, "total": 30},
            {"acct": 2, "total": 45},
        ]
        # --8<-- [end:grouped]

        # --8<-- [start:grouped-traversed]
        # Group keys may traverse. The account-less transaction drops out,
        # exactly as it would from a where() predicate on the same path.
        rows = await (
            Transaction.select(
                lambda transaction: {
                    "account_label": transaction.account.label,
                    "total": transaction.amount.sum(),
                }
            )
            .order_by("account_label")
            .all()
        )
        assert rows.model_dump() == [
            {"account_label": "Checking", "total": 30},
            {"account_label": "Savings", "total": 45},
        ]
        # --8<-- [end:grouped-traversed]

        # --8<-- [start:none-bucket]
        # left_join() keeps the account-less transaction, so "no account"
        # becomes a group keyed None instead of a dropped row.
        rows = await (
            Transaction.select(
                lambda transaction: {
                    "account_label": transaction.account.label,
                    "total": transaction.amount.sum(),
                }
            )
            .left_join(lambda transaction: transaction.account)
            .all()
        )
        buckets = {row.account_label: row.total for row in rows}
        assert buckets == {"Checking": 30, "Savings": 45, None: 40}
        # --8<-- [end:none-bucket]

        # --8<-- [start:top-n]
        # The top-N idiom: keys and aggregates, ordered by the aggregate's
        # output name, with limit() applied to the groups.
        top = await (
            Transaction.select(
                lambda transaction: {
                    "account_label": transaction.account.label,
                    "total": transaction.amount.sum(),
                }
            )
            .order_by("total", "desc")
            .limit(1)
            .all()
        )
        assert top.model_dump() == [{"account_label": "Savings", "total": 45}]
        # --8<-- [end:top-n]

        # --8<-- [start:order-by-lambda]
        # order_by()'s lambda form spells the source expression, aggregate
        # included. The largest total comes first.
        rows = await (
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            )
            .where(lambda transaction: transaction.account_id != None)  # noqa: E711
            .order_by(lambda transaction: transaction.amount.sum(), "desc")
            .all()
        )
        assert [(row.acct, row.total) for row in rows] == [(2, 45), (1, 30)]
        # --8<-- [end:order-by-lambda]

        # --8<-- [start:zero-groups]
        # A grouped query over zero rows returns zero records.
        rows = await (
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            )
            .where(lambda transaction: transaction.amount > 10_000)
            .all()
        )
        assert len(rows) == 0
        # --8<-- [end:zero-groups]

        # --8<-- [start:decimal-contract]
        # avg() over a Decimal column stays Decimal, on SQLite and Postgres.
        row = await (
            Transaction.select(
                lambda transaction: {"avg_price": transaction.price.avg()}
            )
            .where(lambda transaction: transaction.price != None)  # noqa: E711
            .first()
        )
        assert row is not None
        assert isinstance(row.avg_price, Decimal)
        assert row.avg_price == Decimal("3")
        # --8<-- [end:decimal-contract]

        # --8<-- [start:error-family]
        # sum() over a string column has no portable meaning.
        try:
            Transaction.select(lambda transaction: {"x": transaction.memo.sum()})
        except TypeError as exc:
            assert "string-typed" in str(exc)
        else:
            raise AssertionError("expected TypeError")
        # --8<-- [end:error-family]

        # --8<-- [start:error-where]
        # An aggregate inside where() raises and points at having().
        try:
            Transaction.where(lambda transaction: transaction.amount.sum() > 100)
        except TypeError as exc:
            assert "having()" in str(exc)
        else:
            raise AssertionError("expected TypeError")
        # --8<-- [end:error-where]

        # --8<-- [start:error-builtin-sum]
        # Python's builtin sum() over a column reference raises.
        try:
            Transaction.select(
                lambda transaction: {
                    "x": sum(transaction.amount)  # type: ignore[arg-type]
                }
            )
        except TypeError as exc:
            assert "did you mean t.amount.sum()?" in str(exc)
        else:
            raise AssertionError("expected TypeError")
        # --8<-- [end:error-builtin-sum]

        # --8<-- [start:error-unnamed]
        # An aggregate outside the dict form has no output name.
        try:
            Transaction.select(lambda transaction: transaction.amount.sum())
        except TypeError as exc:
            assert "give t.amount.sum() an output name" in str(exc)
        else:
            raise AssertionError("expected TypeError")
        # --8<-- [end:error-unnamed]

        # --8<-- [start:error-order-key]
        # Sorting groups by a column that is not a group key raises.
        try:
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            ).order_by("memo")
        except ValueError as exc:
            assert "group key or an aggregate" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        # --8<-- [end:error-order-key]

        # --8<-- [start:error-count]
        # count() on an aggregate projection is ambiguous: rows or groups?
        try:
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            ).count()
        except ValueError as exc:
            assert "len(await q.all())" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        # --8<-- [end:error-count]

    print("aggregations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
