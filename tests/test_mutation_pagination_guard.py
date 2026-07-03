"""FF-A A1 (#171): limit/offset on mutating queries raise instead of being ignored."""

from typing import Annotated

import pytest

from ferro import FerroField, Model, connect, engines


class PaginationGuardItem(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    flagged: bool = False


@pytest.mark.asyncio
async def test_delete_with_limit_raises_value_error():
    with pytest.raises(ValueError, match=r"delete\(\) does not support limit/offset"):
        await PaginationGuardItem.where(lambda item: item.flagged == True).limit(  # noqa: E712
            5
        ).delete()


@pytest.mark.asyncio
async def test_delete_with_offset_raises_value_error():
    with pytest.raises(ValueError, match=r"delete\(\) does not support limit/offset"):
        await PaginationGuardItem.where(lambda item: item.flagged == True).offset(  # noqa: E712
            2
        ).delete()


@pytest.mark.asyncio
async def test_update_with_limit_raises_value_error():
    with pytest.raises(ValueError, match=r"update\(\) does not support limit/offset"):
        await PaginationGuardItem.where(lambda item: item.flagged == True).limit(  # noqa: E712
            5
        ).update(name="renamed")


@pytest.mark.asyncio
async def test_update_with_offset_raises_value_error():
    with pytest.raises(ValueError, match=r"update\(\) does not support limit/offset"):
        await PaginationGuardItem.where(lambda item: item.flagged == True).offset(  # noqa: E712
            2
        ).update(name="renamed")


def test_mutating_query_def_omits_pagination_keys():
    query = PaginationGuardItem.where(lambda item: item.flagged == True)  # noqa: E712
    for operation in ("update", "delete"):
        payload = query._mutating_query_def(operation)
        assert "limit" not in payload
        assert "offset" not in payload
        assert payload["model_name"] == "PaginationGuardItem"
        assert payload["order_by"] == []
        assert payload["m2m"] is None
        assert len(payload["where"]) == 1


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_failed_paginated_mutation_touches_no_rows(db_url):
    class GuardedRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():

        for label in ("a", "b", "c"):
            await GuardedRow(label=label).save()

        with pytest.raises(ValueError):
            await GuardedRow.where(lambda row: row.id > 0).limit(1).delete()
        with pytest.raises(ValueError):
            await GuardedRow.where(lambda row: row.id > 0).offset(1).update(label="x")

        rows = await GuardedRow.where(lambda row: row.id > 0).order_by("label").all()
        assert [row.label for row in rows] == ["a", "b", "c"]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_first_then_delete_on_same_query_still_allowed(db_url):
    class FirstThenDelete(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():

        await FirstThenDelete(label="only").save()

        query = FirstThenDelete.where(lambda row: row.label == "only")
        found = await query.first()
        assert found is not None

        deleted = await query.delete()
        assert deleted == 1
