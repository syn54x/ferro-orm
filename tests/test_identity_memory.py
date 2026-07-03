"""FF-D D1a exit gate: the identity map is memory-bounded.

The map holds weak references: instances are released when user code drops
them; a dead entry is a miss. This is the deterministic form of the roadmap's
"bounded RSS after GC" gate — live-entry count and weakref death prove the
bound without RSS flakiness. (Strong-ref map fails both tests today.)
"""

import asyncio
import gc
import weakref

import pytest

from ferro import Field, Model, connect, engines
from ferro._core import identity_map_len


@pytest.mark.asyncio
async def test_dropped_instances_are_released_by_the_map(db_url):
    class MemItem(Model):
        id: int | None = Field(default=None, primary_key=True)
        payload: str

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        created = await MemItem.create(payload="x")
        pk = created.id
        ref = weakref.ref(created)

        del created
        gc.collect()
        # The map must not keep the instance alive.
        assert ref() is None

        # A dead entry is a miss: re-fetch hydrates a fresh, correct instance.
        fresh = await MemItem.get(pk)
        assert fresh.id == pk
        assert fresh.payload == "x"


@pytest.mark.asyncio
async def test_identity_map_is_bounded_under_bulk_scanning(db_url):
    class MemScan(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        await MemScan.bulk_create([MemScan(n=i) for i in range(20_000)])
        for _ in range(3):
            rows = await MemScan.all()
            assert len(rows) == 20_000
            del rows
            gc.collect()
            # pyo3-async-runtimes resolves the awaited future from a Tokio
            # worker thread via `call_soon_threadsafe`; the done-callback that
            # drops the bridge Future's last strong ref to the result is
            # itself scheduled with `call_soon` and only runs on the *next*
            # event-loop iteration. One uncontended `sleep(0)` flushes that
            # tick so refcounting (not gc) frees the instances — this is a
            # property of the cross-thread bridge, not of the identity map.
            await asyncio.sleep(0)
            gc.collect()
            # Live entries collapse once user refs are gone — the map is
            # bounded by *live* instances, not by rows ever loaded.
            assert identity_map_len(s.session_id) < 100


@pytest.mark.asyncio
async def test_identity_dedup_still_works_for_live_instances(db_url):
    class MemLive(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        a = await MemLive.create(n=1)
        b = await MemLive.get(a.id)
        assert b is a  # weak map still dedupes while the instance is alive
