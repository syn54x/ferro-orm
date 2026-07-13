"""Type-checked (never executed): projected-query chains must PASS `ty check`.

test_static_contracts.py asserts this file produces no diagnostics — the
positive half of the projection gate (#279/#293): every selector form (tuple,
single-field, dict-aliased, traversed at depth) yields a ``ProjectedQuery``
whose ``all()`` is ``Rows[Row]`` and ``first()`` is ``Row | None``.
"""

from typing import Annotated

from ferro import FerroField, ForeignKey, Model
from ferro.query import Row, Rows


class GoodPOwner(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    email: str = ""


class GoodPAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str = ""
    owner: Annotated[GoodPOwner | None, ForeignKey(related_name="accounts")] = None


class GoodPTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[GoodPAccount | None, ForeignKey(related_name="txns")] = None


async def _tuple_selector() -> None:
    rows: Rows[Row] = await GoodPTxn.select(lambda t: (t.id, t.amount)).all()
    del rows


async def _single_field_selector() -> None:
    one: Row | None = await GoodPTxn.select(lambda t: t.amount).first()
    del one


async def _traversed_tuple_selector() -> None:
    # Traversal at depth 2 (#293): each hop types as FieldProxy[Any], the
    # chain stays selector-shaped, and the result is still Rows[Row].
    rows: Rows[Row] = await GoodPTxn.select(
        lambda t: (t.amount, t.account.name, t.account.owner.email)
    ).all()
    del rows


async def _dict_selector_with_aliases() -> None:
    # The dict-returning selector (#293): string keys name output fields.
    rows: Rows[Row] = await GoodPTxn.select(
        lambda t: {
            "txn_id": t.id,
            "account_name": t.account.name,
            "owner_email": t.account.owner.email,
        }
    ).all()
    del rows


async def _projection_composes_with_left_join() -> None:
    rows: Rows[Row] = await (
        GoodPTxn.select(lambda t: {"account_name": t.account.name})
        .left_join(lambda t: t.account)
        .order_by("id")
        .all()
    )
    del rows


async def _aggregate_dict_selector() -> None:
    # Aggregate expressions (#294) are dict-selector values; they type
    # opaquely (AggregateExpr) and the result stays Rows[Row] / Row | None —
    # shape-not-names typing holds (record-field inference is #290's lane).
    one: Row | None = await GoodPTxn.select(
        lambda t: {
            "n": t.id.count(),
            "total": t.amount.sum(),
            "mean": t.amount.avg(),
            "lo": t.amount.min(),
            "hi": t.amount.max(),
            "avg_traversed": t.account.owner.id.avg(),
        }
    ).first()
    del one
