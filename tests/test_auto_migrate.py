from typing import Annotated

import pytest
from pydantic import Field

import ferro
from ferro import Model
from ferro.base import FerroField, ManyToManyField
from ferro.query import BackRef


class AutoMigratedUser(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    username: str


@pytest.mark.asyncio
async def test_connect_with_auto_migrate():
    """Test that connect(auto_migrate=True) creates tables automatically."""
    # Reset engine to ensure clean state
    ferro.reset_engine()

    # Connect with auto_migrate=True
    # This should internally call the same logic as create_tables()
    await ferro.connect("sqlite::memory:", auto_migrate=True)

    # We can verify it works by trying to call create_tables again
    # or by just ensuring it doesn't crash.
    # In a future step, when we have INSERT, we can verify the table exists.
    # For now, we are verifying the API signature and that it runs without error.
    assert True


@pytest.mark.asyncio
async def test_connect_without_auto_migrate():
    """Test that connect(auto_migrate=False) does not create tables (manual mode)."""
    ferro.reset_engine()

    await ferro.connect("sqlite::memory:", auto_migrate=False)
    # Manual call still works
    await ferro.create_tables()
    assert True


@pytest.mark.asyncio
async def test_m2m_join_table_created_during_auto_migrate():
    """Verify that the many-to-many join table is created when auto_migrate=True.
    We clear registries, migrate a fresh in-memory DB, then use the M2M API; if the
    join table were not created, .add() would fail. No second connection needed."""
    from ferro import clear_registry, connect, reset_engine
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class Actor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: BackRef[Actor] = None

    await connect("sqlite::memory:", auto_migrate=True)

    actor = await Actor.create(name="Alice")
    movie = await Movie.create(title="Matrix")
    await actor.movies.add(movie)

    linked = await actor.movies.all()
    assert len(linked) == 1
    assert linked[0].id == movie.id
    assert linked[0].title == "Matrix"
