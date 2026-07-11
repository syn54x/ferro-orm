"""Type-checked (never executed): projected-shape misuse must FAIL `ty check`.

test_static_contracts.py asserts this file produces `invalid-assignment`
diagnostics -- if it starts passing, the projected-shape gate has stopped
biting (#279).
"""

from typing import Annotated

from ferro import FerroField, ForeignKey, Model


class BadPAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None


class BadPTxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[BadPAccount, ForeignKey(related_name="txns")]


async def _row_where_model_expected() -> None:
    # A projected query's results are Row records, never model instances —
    # the complete-instance invariant extends through static typing
    # (ADR-0007). Both assignments fail with invalid-assignment.
    txns: list[BadPTxn] = await BadPTxn.select(lambda t: (t.id, t.amount)).all()

    one: BadPTxn | None = await BadPTxn.select(lambda t: t.id).first()

    del txns, one
