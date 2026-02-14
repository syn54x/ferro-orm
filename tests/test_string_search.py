import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_search_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_like_search(db_url):
    """Test string searching with .like()."""

    class SearchableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)

    await SearchableUser.create(name="Taylor")
    await SearchableUser.create(name="Tyler")

    # .like()
    results = await SearchableUser.where(SearchableUser.name.like("Tay%")).all()
    assert len(results) >= 1
    assert any(r.name == "Taylor" for r in results)


@pytest.mark.asyncio
async def test_in_helper(db_url):
    """Test .in_() as an alternative to <<."""

    class SearchableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)

    await SearchableUser.create(name="user1")
    await SearchableUser.create(name="user2")
    await SearchableUser.create(name="user3")

    # Use .in_()
    results = await SearchableUser.where(
        SearchableUser.name.in_(["user1", "user3"])
    ).all()
    assert len(results) == 2
    assert {r.name for r in results} == {"user1", "user3"}

    # Verify << still works
    results_legacy = await SearchableUser.where(
        SearchableUser.name << ["user1", "user3"]
    ).all()
    assert len(results_legacy) == 2
