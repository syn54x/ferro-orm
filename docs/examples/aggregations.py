"""Runnable companion to the "Aggregations & Grouped Queries" guide
(docs/pages/guide/aggregations.md).

Seeds a small transactions table, then runs every aggregate query the guide
shows and asserts the exact records that come back.
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
    price: Decimal | None = Field(default=None)
    memo: str
    account: Annotated[
        Account | None, ForeignKey(related_name="transactions")
    ] = None
# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        a1 = await Account.create(id=1, label="a1")
        b1 = await Account.create(id=2, label="b1")
        await Transaction.create(
            id=1, amount=10, price=Decimal("1.25"), memo="coffee", account=a1
        )
        await Transaction.create(
            id=2, amount=20, price=Decimal("2.75"), memo="lunch", account=a1
        )
        await Transaction.create(
            id=3, amount=30, price=Decimal("6.00"), memo="dinner", account=b1
        )
        await Transaction.create(id=4, amount=40, memo="orphan")  # no account

        # --8<-- [start:global]
        # An aggregate-only projection collapses to exactly one record:
        # the global aggregate, read idiomatically with first().
        row = await Transaction.select(
            lambda t: {
                "n": t.id.count(),
                "total": t.amount.sum(),
                "average": t.amount.avg(),
                "smallest": t.amount.min(),
                "largest": t.amount.max(),
            }
        ).first()

        assert row is not None
        assert row.model_dump() == {
            "n": 4,
            "total": 100,
            "average": 25.0,
            "smallest": 10,
            "largest": 40,
        }
        # --8<-- [end:global]

        # --8<-- [start:global-where]
        # Aggregates measure whatever where() leaves in — traversal included.
        row = await (
            Transaction.select(lambda t: {"total": t.amount.sum()})
            .where(lambda t: t.account.label == "a1")
            .first()
        )
        assert row is not None and row.total == 30
        # --8<-- [end:global-where]

        # --8<-- [start:empty]
        # Over zero matching rows, SQL's own empty-input semantics pass
        # through verbatim: one record, None for sum/avg/min/max, 0 for
        # count. No hidden COALESCE — "sum of no rows" and "sum of rows
        # totaling zero" stay distinguishable.
        row = await (
            Transaction.select(
                lambda t: {"n": t.id.count(), "total": t.amount.sum()}
            )
            .where(lambda t: t.amount > 10_000)
            .first()
        )
        assert row is not None
        assert row.n == 0
        assert row.total is None
        # --8<-- [end:empty]

        # --8<-- [start:count-nulls]
        # COUNT(column) counts non-NULL values of that column — SQL
        # semantics, verbatim. Only one of four transactions has a price...
        row = await Transaction.select(
            lambda t: {"rows": t.id.count(), "priced": t.price.count()}
        ).first()
        assert row is not None
        assert row.rows == 4
        assert row.priced == 3
        # --8<-- [end:count-nulls]

        # --8<-- [start:grouped]
        # Mix a plain field in and the query becomes GROUPED: every
        # non-aggregate field is a group key. There is no group_by() —
        # the record shape IS the grouping.
        rows = await (
            Transaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.account_id != None)  # noqa: E711
            .order_by("acct")
            .all()
        )
        assert rows.model_dump() == [
            {"acct": 1, "total": 30},
            {"acct": 2, "total": 30},
        ]
        # --8<-- [end:grouped]

        # --8<-- [start:grouped-traversed]
        # Group keys may traverse. Traversal narrows (the account-less
        # transaction drops out) exactly like a where() predicate would.
        rows = await (
            Transaction.select(
                lambda t: {"account_label": t.account.label, "total": t.amount.sum()}
            )
            .order_by("account_label")
            .all()
        )
        assert rows.model_dump() == [
            {"account_label": "a1", "total": 30},
            {"account_label": "b1", "total": 30},
        ]
        # --8<-- [end:grouped-traversed]

        # --8<-- [start:none-bucket]
        # left_join() keeps relation-less rows, so "has no account" becomes
        # a visible None-keyed group instead of a dropped row.
        rows = await (
            Transaction.select(
                lambda t: {"account_label": t.account.label, "total": t.amount.sum()}
            )
            .left_join(lambda t: t.account)
            .all()
        )
        buckets = {row.account_label: row.total for row in rows}
        assert buckets == {"a1": 30, "b1": 30, None: 40}
        # --8<-- [end:none-bucket]

        # --8<-- [start:top-n]
        # The top-N idiom: keys + aggregates, order by the aggregate's
        # output name, limit the groups.
        top = await (
            Transaction.select(
                lambda t: {"account_label": t.account.label, "total": t.amount.sum()}
            )
            .order_by("total", "desc")
            .limit(1)
            .all()
        )
        assert len(top) == 1
        # --8<-- [end:top-n]

        # --8<-- [start:order-by-lambda]
        # order_by's lambda form spells the source expression — including
        # the aggregate itself.
        rows = await (
            Transaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.account_id != None)  # noqa: E711
            .order_by(lambda t: t.amount.sum(), "desc")
            .order_by("acct")
            .all()
        )
        assert [row.acct for row in rows] == [1, 2]
        # --8<-- [end:order-by-lambda]

        # --8<-- [start:zero-groups]
        # A grouped query over zero rows returns zero records — unlike a
        # global aggregate, there is no group to report on.
        rows = await (
            Transaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            )
            .where(lambda t: t.amount > 10_000)
            .all()
        )
        assert len(rows) == 0
        # --8<-- [end:zero-groups]

        # --8<-- [start:decimal-contract]
        # Result types are a pinned cross-backend contract derived from the
        # source column: avg over a Decimal column stays Decimal (never a
        # silently lossy float), on SQLite and Postgres alike.
        row = await (
            Transaction.select(lambda t: {"avg_price": t.price.avg()})
            .where(lambda t: t.price != None)  # noqa: E711
            .first()
        )
        assert row is not None
        assert isinstance(row.avg_price, Decimal)
        # --8<-- [end:decimal-contract]

        # --8<-- [start:errors]
        # The loud limits, all at build time — before any SQL:

        # Aggregates over families with no portable cross-backend meaning.
        try:
            Transaction.select(lambda t: {"x": t.memo.sum()})
        except TypeError as exc:
            assert "string-typed" in str(exc)

        # Aggregates inside where() — filtering after aggregation is
        # having(), which is not built yet.
        try:
            Transaction.where(lambda t: t.amount.sum() > 100)
        except TypeError as exc:
            assert "having()" in str(exc)

        # The builtin-sum trap: aggregation is a method on the column.
        try:
            Transaction.select(lambda t: {"x": sum(t.amount)})  # type: ignore[arg-type]
        except TypeError as exc:
            assert "did you mean t.amount.sum()?" in str(exc)

        # Sorting a grouped query by a column that is not a group key would
        # let each group answer with an arbitrary row's value.
        try:
            Transaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            ).order_by("memo")
        except ValueError as exc:
            assert "group key or an aggregate" in str(exc)

        # count()/exists() on an aggregate projection are ambiguous between
        # rows and groups; both spellings are in the error.
        try:
            Transaction.select(
                lambda t: {"acct": t.account_id, "total": t.amount.sum()}
            ).count()
        except ValueError as exc:
            assert "len(await q.all())" in str(exc)
        # --8<-- [end:errors]

    print("aggregations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
