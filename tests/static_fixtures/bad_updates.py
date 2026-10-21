"""Type-checked (never executed): mixing `update()`'s two forms must FAIL `ty check`.

`update()` takes a recipe callable or keyword literals, never both, and so
does `update.sql()`, which takes the terminal's own arguments.
test_static_contracts.py asserts this file produces one `no-matching-overload`
diagnostic per misuse. If it starts passing, the overloads have stopped
biting.
"""

from typing import Annotated

from ferro import FerroField, Model
from ferro.query import Query


class BadUAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    visits: int = 0


async def _mixed_forms_on_the_call() -> None:
    await Query(BadUAccount).update(lambda account: {"visits": 1}, visits=1)


async def _mixed_forms_on_the_rendering() -> None:
    await Query(BadUAccount).update.sql(lambda account: {"visits": 1}, visits=1)
