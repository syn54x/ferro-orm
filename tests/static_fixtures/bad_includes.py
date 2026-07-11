"""Type-checked (never executed): include misuse must FAIL `ty check` (#287).

test_static_contracts.py asserts this file produces `invalid-argument-type`
diagnostics — include on a projected query (the `self: Never` pin, one
materialization plan per query / #282) and a string selector (strings never
traverse, #280) are static errors at the call site. One misuse per function —
an earlier NoReturn await would mark the rest of the body unreachable and
suppress its diagnostic.
"""

from typing import Annotated

from ferro import FerroField, ForeignKey, Model


class BadIAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str = ""


class BadITxn(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    amount: int = 0
    account: Annotated[BadIAccount, ForeignKey(related_name="txns")]


async def _include_on_a_projection() -> None:
    # select(columns…) then include(): the projected query's include() takes
    # `self: Never`, so the call site reports invalid-argument-type.
    await BadITxn.select(lambda t: (t.id,)).include(lambda t: t.account).all()


async def _string_include_selector() -> None:
    # include("account"): strings never traverse (#280) — the selector
    # parameter is a callable, so a str argument is invalid-argument-type.
    await BadITxn.select().include("account").all()
