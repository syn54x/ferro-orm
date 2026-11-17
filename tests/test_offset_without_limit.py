"""`offset()` without `limit()` skips rows and returns the rest on both backends (#756)."""

import logging

import pytest

import ferro
from ferro import Model


class OffsetItem(Model):
    id: int | None = ferro.Field(default=None, primary_key=True)
    name: str


async def _save_rows() -> None:
    for pk, name in ((1, "a"), (2, "b"), (3, "c")):
        await OffsetItem(id=pk, name=name).save()


def _ordered():
    return OffsetItem.select().order_by(lambda offset_item: offset_item.id)


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_a_bare_offset_returns_the_rest(db_url):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _save_rows()
        rows = await _ordered().offset(1).all()
    assert [row.id for row in rows] == [2, 3]


@pytest.mark.backend_matrix
@pytest.mark.asyncio
@pytest.mark.parametrize(("offset", "ids"), [(0, [1, 2, 3]), (3, [])])
async def test_a_bare_offset_at_the_edges(db_url, offset, ids):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _save_rows()
        rows = await _ordered().offset(offset).all()
    assert [row.id for row in rows] == ids


@pytest.mark.backend_matrix
@pytest.mark.asyncio
async def test_the_rendered_bare_offset_is_the_logged_statement(db_url, caplog):
    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await _save_rows()
        rendered = await _ordered().offset(1).all.sql()
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="ferro.sql"):
            await _ordered().offset(1).all()
    records = [r for r in caplog.records if r.name == "ferro.sql"]
    assert records
    assert rendered.sql == records[-1].sql
