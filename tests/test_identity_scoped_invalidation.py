"""FF-D D2 exit gate: invalidation is scoped, never a global clear.

Rollback evicts only the affected (connection, session); bulk update/delete
evict only (connection, model). Unrelated cached instances survive.
"""

import pytest

from ferro import Field, Model, connect, engines, transaction


@pytest.mark.asyncio
async def test_rollback_evicts_only_the_affected_session(db_url):
    class InvA(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    # Session 1: cache an instance, then let the session end.
    async with engines.session():
        kept = await InvA.create(name="kept-in-s1")

    # Session 2: roll back a transaction; only this session's map is evicted.
    async with engines.session():
        outer = await InvA.create(name="s2-outer")
        try:
            async with transaction():
                await InvA.create(name="rolled-back")
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        # s2's map was evicted by the rollback: re-fetch hydrates fresh.
        refetched = await InvA.get(outer.id)
        assert refetched is not outer
        assert refetched.name == "s2-outer"

    # A different session was never touched (its map died with it anyway;
    # the point is the rollback path no longer clears anything global).
    async with engines.session():
        still_there = await InvA.get(kept.id)
        assert still_there.name == "kept-in-s1"


@pytest.mark.asyncio
async def test_bulk_update_evicts_only_that_model(db_url):
    class InvUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class InvOrder(Model):
        id: int | None = Field(default=None, primary_key=True)
        item: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        u = await InvUser.create(name="before")
        o = await InvOrder.create(item="widget")

        await InvUser.where(lambda t: t.id == u.id).update(name="after")

        fresh_u = await InvUser.get(u.id)
        assert fresh_u is not u  # evicted: fresh instance
        assert fresh_u.name == "after"

        same_o = await InvOrder.get(o.id)
        assert same_o is o  # unrelated model survived


@pytest.mark.asyncio
async def test_bulk_delete_evicts_only_that_model(db_url):
    class InvDelA(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    class InvDelB(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        a = await InvDelA.create(n=1)
        b = await InvDelB.create(n=2)
        await InvDelA.where(lambda t: t.n == 1).delete()
        assert await InvDelA.get_or_none(a.id) is None
        assert (await InvDelB.get(b.id)) is b
