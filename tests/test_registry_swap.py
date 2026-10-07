"""``Registry.swap`` (#530): a historical modelset installed for a block,
today's restored on exit, error and cancellation.

A data step queries ``ctx.models.Author``, a class built from the migration's
snapshots. While it runs, the registry (Python and Rust) holds those classes
instead of today's, and today's ``Author`` refuses to be queried::

    with REGISTRY.swap(historical_models):
        await historical_models.Author.where(lambda author: author.slug == None).all()
        Author.where(...)   # SwappedOutModelError: ... use ctx.models.Author ...
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

import ferro
from ferro import FerroField, Model
from ferro.migrations import historical
from ferro.registry import REGISTRY, SwappedOutModelError

pytestmark = pytest.mark.usefixtures("clean_registry")


def _snapshot() -> dict:
    """A snapshot whose ``author`` has ``slug``, which today's model lacks."""
    column = {
        "primary_key": False,
        "autoincrement": False,
        "unique": False,
        "index": False,
        "default": None,
        "format": None,
    }
    return {
        "ir_kind": "schema",
        "ir_version": 2,
        "payload": {
            "dialect_agnostic": True,
            "models": [
                {
                    "model_name": "app.models.Author",
                    "table_name": "author",
                    "columns": [
                        {
                            **column,
                            "name": "id",
                            "logical_type": "integer",
                            "nullable": False,
                            "primary_key": True,
                            "autoincrement": True,
                        },
                        {
                            **column,
                            "name": "name",
                            "logical_type": "string",
                            "nullable": False,
                        },
                        {
                            **column,
                            "name": "slug",
                            "logical_type": "string",
                            "nullable": True,
                        },
                    ],
                    "foreign_keys": [],
                    "indexes": [],
                    "uniques": [],
                    "checks": [],
                }
            ],
        },
    }


def _today() -> type[Model]:
    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    return Author


def test_swap_installs_the_modelset_and_restores_today_on_exit():
    author = _today()
    models = historical.build_single(_snapshot(), "0002_add_slug")
    today = dict(REGISTRY.models())

    with REGISTRY.swap(models):
        installed = REGISTRY.models()
        assert list(installed.values()) == [models.Author]
        assert models.Author.__ferro_identity__ == (
            "ferro.migrations.historical.0002_add_slug.Author"
        )
        with pytest.raises(SwappedOutModelError, match=r"use ctx\.models\.Author"):
            author.where(lambda author: author.name == "x")
        with pytest.raises(SwappedOutModelError, match=r"ctx\.models\.Author"):
            author(name="x").save  # noqa: B018 - an attribute access is the query

    assert REGISTRY.models() == today
    assert author.where(lambda author: author.name == "x") is not None
    assert "where" not in author.__dict__


def test_swap_restores_today_when_the_block_raises():
    author = _today()
    models = historical.build_single(_snapshot(), "0002_add_slug")
    today = dict(REGISTRY.models())

    with pytest.raises(RuntimeError, match="the step failed"):
        with REGISTRY.swap(models):
            raise RuntimeError("the step failed")

    assert REGISTRY.models() == today
    assert author.where(lambda author: author.name == "x") is not None


@pytest.mark.asyncio
async def test_swap_restores_today_when_the_task_is_cancelled():
    author = _today()
    models = historical.build_single(_snapshot(), "0002_add_slug")
    today = dict(REGISTRY.models())
    entered = asyncio.Event()

    async def step() -> None:
        with REGISTRY.swap(models):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(step())
    await entered.wait()
    assert list(REGISTRY.models().values()) == [models.Author]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert REGISTRY.models() == today
    assert author.where(lambda author: author.name == "x") is not None


@pytest.mark.asyncio
async def test_the_rust_runtime_queries_the_installed_modelset(tmp_path):
    """Inside the swap the historical class hydrates the column today's model
    lacks; after it, today's model queries its own shape again."""
    author = _today()
    await ferro.connect(f"sqlite:{tmp_path / 'swap.db'}?mode=rwc", name="swap")
    async with ferro.transaction(using="swap") as tx:
        await tx.execute(
            "CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT)"
        )
        await tx.execute("INSERT INTO author (name, slug) VALUES ('Ann', 'ann')")

    models = historical.build_single(_snapshot(), "0002_add_slug")
    with REGISTRY.swap(models):
        async with ferro.transaction(using="swap"):
            [row] = await models.Author.where(lambda author: author.slug == "ann").all()
            assert (row.name, row.slug) == ("Ann", "ann")

    async with ferro.transaction(using="swap"):
        [row] = await author.where(lambda author: author.name == "Ann").all()
    assert type(row) is author
    assert row.name == "Ann"
