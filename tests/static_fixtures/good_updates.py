"""Type-checked (never executed): each of `update()`'s forms must PASS `ty check`.

The recipe form and the keyword form type-check on the call and on
`update.sql()`, with the call's and the rendering's own return types.
"""

from typing import Annotated, assert_type

from ferro import FerroField, Model, RenderedSql
from ferro.query import Query


class GoodUAccount(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    visits: int = 0


async def _each_form() -> None:
    query = Query(GoodUAccount).where(lambda account: account.id == 1)
    assert_type(await query.update(visits=2), int)
    assert_type(await query.update(lambda account: {"visits": account.visits + 1}), int)
    assert_type(await query.update.sql(visits=2), RenderedSql)
    assert_type(
        await query.update.sql(lambda account: {"visits": account.visits + 1}),
        RenderedSql,
    )
