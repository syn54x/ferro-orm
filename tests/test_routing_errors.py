"""FF-D D4 + ambient-default removal (v0.13, one minor ahead of the notice).

Every operation needs an explicit route: a session (ambient or session=) or
using=. The silent using-bypasses-session path is a ValueError.
"""

import pytest

from ferro import Field, Model, connect, engines, execute


@pytest.mark.asyncio
async def test_operation_with_no_route_raises(db_url):
    class RtA(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    with pytest.raises(RuntimeError, match="No database route"):
        await RtA.all()


@pytest.mark.asyncio
async def test_raw_with_no_route_raises(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(RuntimeError, match="No database route"):
        await execute("SELECT 1")


@pytest.mark.asyncio
async def test_using_matching_ambient_session_is_allowed(db_url):
    class RtB(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        a = await RtB.create(name="x")
        b = await RtB.all(using=s.connection_name)
        assert b[0] is a  # same connection: session-scoped, not a bypass


@pytest.mark.asyncio
async def test_using_conflicting_with_ambient_session_raises(db_url, tmp_path):
    class RtC(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    await connect(f"sqlite:{tmp_path}/other.db?mode=rwc", auto_migrate=True, name="other")
    async with engines.session():
        with pytest.raises(ValueError, match="conflicts with the ambient session"):
            await RtC.all(using="other")


@pytest.mark.asyncio
async def test_using_alone_still_works_without_identity(db_url, tmp_path):
    class RtD(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(f"sqlite:{tmp_path}/named.db?mode=rwc", auto_migrate=True, name="named")
    rows = await RtD.all(using="named")
    assert rows == []  # runs fine sessionless; no identity map involved
