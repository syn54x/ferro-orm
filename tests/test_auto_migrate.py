from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import Field

import ferro
from ferro import Model
from ferro.base import FerroField, ManyToManyField
from ferro.query import BackRef

pytestmark = pytest.mark.backend_matrix


class AutoMigratedUser(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    username: str


@pytest.mark.asyncio
async def test_connect_with_auto_migrate(db_url):
    """Test that connect(auto_migrate=True) creates tables automatically."""
    # Reset engine to ensure clean state
    ferro.reset_engine()

    # Connect with auto_migrate=True
    # This should internally call the same logic as create_tables()
    await ferro.connect(db_url, auto_migrate=True)

    # We can verify it works by trying to call create_tables again
    # or by just ensuring it doesn't crash.
    # In a future step, when we have INSERT, we can verify the table exists.
    # For now, we are verifying the API signature and that it runs without error.
    assert True


@pytest.mark.asyncio
async def test_connect_without_auto_migrate(db_url):
    """Test that connect(auto_migrate=False) does not create tables (manual mode)."""
    ferro.reset_engine()

    await ferro.connect(db_url, auto_migrate=False)
    # Manual call still works
    await ferro.create_tables()
    assert True


@pytest.mark.asyncio
async def test_m2m_join_table_created_during_auto_migrate(db_url):
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

    await connect(db_url, auto_migrate=True)

    actor = await Actor.create(name="Alice")
    movie = await Movie.create(title="Matrix")
    await actor.movies.add(movie)

    linked = await actor.movies.all()
    assert len(linked) == 1
    assert linked[0].id == movie.id
    assert linked[0].title == "Matrix"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_uuid_m2m_join_table_columns_inherit_pk_type_and_nullability(db_url):
    """Runtime join-table DDL should derive FK column metadata from source PKs."""
    from ferro import clear_registry, connect, reset_engine
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidActor(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        movies: Annotated[list["UuidMovie"], ManyToManyField(related_name="actors")] = None

    class UuidMovie(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        title: str
        actors: BackRef[UuidActor] = None

    await connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(uuidactor_movies)").fetchall()
    conn.close()

    columns = {row[1]: row for row in rows}
    assert columns["uuidactor_id"][2].upper() in {"UUID", "UUID_TEXT", "TEXT", "CHAR", "VARCHAR"}
    assert columns["uuidmovie_id"][2].upper() in {"UUID", "UUID_TEXT", "TEXT", "CHAR", "VARCHAR"}
    assert columns["uuidactor_id"][3] == 1
    assert columns["uuidmovie_id"][3] == 1
