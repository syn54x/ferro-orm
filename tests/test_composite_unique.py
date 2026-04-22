"""Composite unique constraints and default M2M join-table uniqueness (TDD)."""

import os
import sqlite3
import uuid
from typing import Annotated, ClassVar

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    FerroField,
    ManyToManyField,
    Model,
    clear_registry,
    connect,
    reset_engine,
)
from ferro.migrations import get_metadata


@pytest.fixture
def db_url():
    db_file = f"test_composite_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.fixture(autouse=True)
def cleanup_registry():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    yield
    reset_engine()
    clear_registry()
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()


def _unique_constraints(table: sa.Table) -> list[sa.UniqueConstraint]:
    return [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]


def test_composite_unique_unknown_column_raises():
    """Invalid __ferro_composite_uniques__ references must fail at class definition time."""

    with pytest.raises(RuntimeError, match="unknown column"):

        class BadPair(Model):
            __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("alpha_id", "nonexistent"),
            )

            id: Annotated[int | None, FerroField(primary_key=True)] = None
            alpha_id: int
            beta_id: int


@pytest.mark.asyncio
async def test_composite_unique_enforced_on_user_model(db_url):
    """Two rows with the same (alpha_id, beta_id) must violate the composite unique."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: Annotated[int | None, FerroField(primary_key=True)] = None
        alpha_id: int
        beta_id: int

    await connect(db_url, auto_migrate=True)

    await PairRow(alpha_id=1, beta_id=2).save()
    with pytest.raises(Exception) as excinfo:
        await PairRow(alpha_id=1, beta_id=2).save()

    msg = str(excinfo.value)
    assert "UNIQUE constraint failed" in msg or "uniqueness" in msg.lower()


@pytest.mark.asyncio
async def test_m2m_duplicate_link_rejected(db_url):
    """Default M2M join table must reject inserting the same pair twice."""

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
    with pytest.raises(Exception) as excinfo:
        await actor.movies.add(movie)
    msg = str(excinfo.value)
    assert "UNIQUE constraint failed" in msg or "uniqueness" in msg.lower()


def test_alembic_metadata_has_unique_constraints():
    """get_metadata() must expose UniqueConstraint for composite user model and M2M join."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: Annotated[int | None, FerroField(primary_key=True)] = None
        alpha_id: int
        beta_id: int

    class Actor(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None

    class Movie(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        title: str
        actors: BackRef[Actor] = None

    metadata = get_metadata()

    pair_table = metadata.tables["pairrow"]
    pair_ucs = _unique_constraints(pair_table)
    assert pair_ucs, "expected at least one UniqueConstraint on pairrow"
    col_names = {tuple(sorted(c.key for c in uc.columns)) for uc in pair_ucs}
    assert ("alpha_id", "beta_id") in col_names

    join_table = metadata.tables["actor_movies"]
    join_ucs = _unique_constraints(join_table)
    assert join_ucs, "expected UniqueConstraint on M2M join table"
    join_col_sets = {tuple(sorted(c.key for c in uc.columns)) for uc in join_ucs}
    assert ("actor_id", "movie_id") in join_col_sets


@pytest.mark.asyncio
async def test_composite_unique_index_exists_in_sqlite(db_url):
    """SQLite should have a unique index spanning both columns."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: Annotated[int | None, FerroField(primary_key=True)] = None
        alpha_id: int
        beta_id: int

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='pairrow' AND sql IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    unique_on_both = any(
        r[1] and "UNIQUE" in r[1].upper() and "alpha_id" in r[1] and "beta_id" in r[1]
        for r in rows
    )
    assert unique_on_both, f"expected unique index on pairrow, got: {rows}"
