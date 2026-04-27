from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import Field

import ferro
from ferro import BackRef, ManyToMany, Model, Relation
from ferro.base import FerroField

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
        movies: Relation[list["Movie"]] = ManyToMany(related_name="actors")

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: Relation[list["Actor"]] = BackRef()

    await connect(db_url, auto_migrate=True)

    actor = await Actor.create(name="Alice")
    movie = await Movie.create(title="Matrix")
    await actor.movies.add(movie)

    linked = await actor.movies.all()
    assert len(linked) == 1
    assert linked[0].id == movie.id
    assert linked[0].title == "Matrix"
    assert await actor.movies.count() == 1

    reverse_linked = await movie.actors.all()
    assert [row.id for row in reverse_linked] == [actor.id]

    await actor.movies.remove(movie)
    assert await actor.movies.count() == 0

    movie_2 = await Movie.create(title="Reloaded")
    await actor.movies.add(movie, movie_2)
    assert await actor.movies.count() == 2
    await actor.movies.clear()
    assert await actor.movies.count() == 0


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
        movies: Relation[list["UuidMovie"]] = ManyToMany(related_name="actors")

    class UuidMovie(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        title: str
        actors: Relation[list["UuidActor"]] = BackRef()

    await connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(uuidactor_movies)").fetchall()
    conn.close()

    columns = {row[1]: row for row in rows}
    assert columns["uuidactor_id"][2].upper() in {
        "UUID",
        "UUID_TEXT",
        "TEXT",
        "CHAR",
        "VARCHAR",
    }
    assert columns["uuidmovie_id"][2].upper() in {
        "UUID",
        "UUID_TEXT",
        "TEXT",
        "CHAR",
        "VARCHAR",
    }
    assert columns["uuidactor_id"][3] == 1
    assert columns["uuidmovie_id"][3] == 1


@pytest.mark.asyncio
async def test_uuid_m2m_relationship_query_serializes_source_id(db_url):
    """UUID source PKs in M2M contexts should serialize for all query operations."""
    from ferro import Field as FerroFieldFn
    from ferro import clear_registry, connect, reset_engine
    from ferro.models import transaction
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()

    class UuidTag(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        name: str = ""
        posts: Relation[list["UuidPost"]] = BackRef()

    class UuidPost(Model):
        id: UUID = FerroFieldFn(default_factory=uuid4, primary_key=True)
        title: str = ""
        tags: Relation[list[UuidTag]] = ManyToMany(related_name="posts")

    await connect(db_url, auto_migrate=True)

    post = await UuidPost.create(title="Hello")
    tag = await UuidTag.create(name="python")

    await post.tags.add(tag)

    linked = await post.tags.all()
    assert [row.id for row in linked] == [tag.id]
    assert await post.tags.count() == 1

    reverse_linked = await tag.posts.all()
    assert [row.id for row in reverse_linked] == [post.id]

    await post.tags.remove(tag)
    assert await post.tags.count() == 0

    tag_2 = await UuidTag.create(name="orm")
    await post.tags.add(tag, tag_2)
    assert await post.tags.count() == 2
    await post.tags.clear()
    assert await post.tags.count() == 0

    async with transaction():
        await post.tags.add(tag)
        assert await post.tags.count() == 1
        await post.tags.remove(tag)
        assert await post.tags.count() == 0

    assert await post.tags.count() == 0
