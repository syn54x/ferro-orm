"""FF-D D1b exit gate: fetch-hits refresh the cached instance in place.

Guarantee: within a session, fetching a row you already hold returns the same
object, updated to the database's current values. The database wins over
unsaved local mutations. (Today the freshly decoded row is discarded.)
"""

import pytest

from ferro import Field, Model, connect, engines, execute

pytestmark = pytest.mark.backend_matrix


def _ph(db_url: str, n: int = 1) -> str:
    """Return the Nth positional placeholder for the active backend."""
    return f"${n}" if "postgres" in db_url else "?"


@pytest.mark.asyncio
async def test_refetch_returns_fresh_values_with_identity_preserved(db_url):
    class RefUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        score: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        u = await RefUser.create(name="old", score=1)

        # External write the ORM cache knows nothing about.
        await execute(
            f"UPDATE refuser SET name = {_ph(db_url, 1)}, "
            f"score = {_ph(db_url, 2)} WHERE id = {_ph(db_url, 3)}",
            "new",
            2,
            u.id,
        )

        again = await RefUser.get(u.id)
        assert again is u          # identity preserved
        assert u.name == "new"     # FAILS today: stale "old"
        assert u.score == 2


@pytest.mark.asyncio
async def test_database_wins_over_unsaved_local_mutation(db_url):
    class RefDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        body: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        d = await RefDoc.create(body="persisted")
        d.body = "unsaved local edit"

        again = await RefDoc.get(d.id)
        assert again is d
        assert d.body == "persisted"  # documented: re-fetch overwrites


@pytest.mark.asyncio
async def test_refresh_resets_fields_set_like_fresh_hydration(db_url):
    class RefFlag(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: str
        b: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        f = await RefFlag.create(a="1", b="2")
        first_fetch = await RefFlag.get(f.id)
        expected_fields_set = set(first_fetch.__pydantic_fields_set__)

        f.a = "mutated"
        again = await RefFlag.get(f.id)
        assert again is f
        assert set(f.__pydantic_fields_set__) == expected_fields_set
        assert f.a == "1"
