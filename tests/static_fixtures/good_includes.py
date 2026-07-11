"""Type-checked (never executed): include chains must PASS `ty check` (#286).

test_static_contracts.py asserts this file produces no diagnostics — an
included query stays ``Query[T]``-shaped (``.all()`` still ``list[T]``, no
distinct loaded type, ADR-0008), and populated access types as the field's
declared annotation.
"""

from typing import Annotated

from ferro import FerroField, ForeignKey, Model


class GIAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""


class GITxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[GIAccount, ForeignKey(related_name="txns")]


async def _include_chain_stays_query_shaped() -> None:
    # include() composes like any chainer and .all() stays list[GITxn] — the
    # shape-not-names promise extends through include.
    txns: list[GITxn] = await (
        GITxn.select()
        .include(lambda t: t.account)
        .where(lambda t: t.amount >= 0)
        .order_by(lambda t: t.amount, "desc")
        .limit(10)
        .all()
    )

    one: GITxn | None = await GITxn.select().include(lambda t: t.account).first()

    # Populated access is a plain attribute matching the declared annotation.
    label: str = txns[0].account.label

    n: int = await GITxn.select().include(lambda t: t.account).count()

    del one, label, n
