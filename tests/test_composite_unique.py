"""Composite unique constraints and default M2M join-table uniqueness (TDD)."""

import sqlite3
from typing import Annotated, ClassVar

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Field,
    ManyToManyField,
    Model,
    clear_registry,
    connect,
    reset_engine,
)
from ferro.migrations import get_metadata

pytestmark = pytest.mark.backend_matrix

def _expected_uq_constraint_name(table_name: str, col_ids: list[str]) -> str:
    """Match Alembic `_build_sa_table` naming (63-char cap)."""
    raw = f"uq_{table_name}_{'_'.join(col_ids)}"
    if len(raw) > 63:
        return raw[:60] + "_uq"
    return raw


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

            id: int | None = Field(default=None, primary_key=True)
            alpha_id: int
            beta_id: int


@pytest.mark.asyncio
async def test_composite_unique_enforced_on_user_model(db_url):
    """Two rows with the same (alpha_id, beta_id) must violate the composite unique."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
        alpha_id: int
        beta_id: int

    await connect(db_url, auto_migrate=True)

    await PairRow(alpha_id=1, beta_id=2).save()
    with pytest.raises(RuntimeError, match="Save failed") as excinfo:
        await PairRow(alpha_id=1, beta_id=2).save()

    msg = str(excinfo.value)
    assert "unique" in msg.lower()


@pytest.mark.asyncio
async def test_m2m_duplicate_link_rejected(db_url):
    """Default M2M join table must reject inserting the same pair twice."""

    class Actor(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None

    class Movie(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        actors: BackRef[Actor] = None

    await connect(db_url, auto_migrate=True)

    actor = await Actor.create(name="Alice")
    movie = await Movie.create(title="Matrix")
    await actor.movies.add(movie)
    with pytest.raises(RuntimeError, match="Add M2M links failed") as excinfo:
        await actor.movies.add(movie)
    msg = str(excinfo.value)
    assert "unique" in msg.lower()


def test_alembic_metadata_has_unique_constraints():
    """get_metadata() must expose UniqueConstraint for composite user model and M2M join."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
        alpha_id: int
        beta_id: int

    class Actor(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        movies: Annotated[list["Movie"], ManyToManyField(related_name="actors")] = None

    class Movie(Model):
        id: int | None = Field(default=None, primary_key=True)
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
@pytest.mark.sqlite_only
async def test_composite_unique_index_exists_in_sqlite(db_url):
    """SQLite should have a unique index spanning both columns."""

    class PairRow(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
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


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_composite_unique_truncated_name_matches_alembic_and_sqlite(db_url):
    """Long uq_* names must be <=63 chars and match between Alembic metadata and Rust-created SQLite indexes."""

    class VeryLongCompositeUniqueModelNameForIndexTruncationTest(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            (
                "very_long_column_name_alpha_for_composite_unique_test",
                "very_long_column_name_beta_for_composite_unique_test",
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        very_long_column_name_alpha_for_composite_unique_test: int
        very_long_column_name_beta_for_composite_unique_test: int

    table = VeryLongCompositeUniqueModelNameForIndexTruncationTest.__name__.lower()
    col_a = "very_long_column_name_alpha_for_composite_unique_test"
    col_b = "very_long_column_name_beta_for_composite_unique_test"
    expected = _expected_uq_constraint_name(table, [col_a, col_b])
    assert len(expected) <= 63, "fixture must exceed 63 before truncation"
    full_raw = f"uq_{table}_{col_a}_{col_b}"
    assert len(full_raw) > 63, "fixture must require truncation"

    metadata = get_metadata()
    ucs = _unique_constraints(metadata.tables[table])
    assert len(ucs) == 1
    assert ucs[0].name == expected

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    )
    rows = cur.fetchall()
    conn.close()

    composite_rows = [
        r
        for r in rows
        if r[1]
        and "UNIQUE" in r[1].upper()
        and col_a in r[1]
        and col_b in r[1]
    ]
    assert composite_rows, f"expected unique composite index on {table}, got: {rows}"
    idx_name = composite_rows[0][0]
    assert idx_name == expected
    assert len(idx_name) <= 63


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_composite_unique_truncated_name_matches_postgres_catalog(
    db_url, postgres_base_url, db_schema_name
):
    """Long uq_* names must match between Alembic metadata and Postgres catalogs."""

    class VeryLongCompositeUniqueModelNameForIndexTruncationTest(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            (
                "very_long_column_name_alpha_for_composite_unique_test",
                "very_long_column_name_beta_for_composite_unique_test",
            ),
        )

        id: int | None = Field(default=None, primary_key=True)
        very_long_column_name_alpha_for_composite_unique_test: int
        very_long_column_name_beta_for_composite_unique_test: int

    table = VeryLongCompositeUniqueModelNameForIndexTruncationTest.__name__.lower()
    col_a = "very_long_column_name_alpha_for_composite_unique_test"
    col_b = "very_long_column_name_beta_for_composite_unique_test"
    expected = _expected_uq_constraint_name(table, [col_a, col_b])

    metadata = get_metadata()
    ucs = _unique_constraints(metadata.tables[table])
    assert len(ucs) == 1
    assert ucs[0].name == expected

    await connect(db_url, auto_migrate=True)

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename = %s
              AND indexname = %s
            """,
            (db_schema_name, table, expected),
        ).fetchone()

    assert row is not None
    idx_name, indexdef = row
    assert idx_name == expected
    assert len(idx_name) <= 63
    assert "UNIQUE" in indexdef.upper()
    assert col_a in indexdef
    assert col_b in indexdef


def test_composite_unique_multiple_groups_in_metadata_and_sqlite():
    """Two disjoint composite groups should yield two UniqueConstraint objects."""

    class MultiGroup(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a_id", "b_id"),
            ("c_id", "d_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
        a_id: int
        b_id: int
        c_id: int
        d_id: int

    metadata = get_metadata()
    table = metadata.tables["multigroup"]
    ucs = _unique_constraints(table)
    assert len(ucs) == 2
    col_sets = {tuple(sorted(c.key for c in uc.columns)) for uc in ucs}
    assert ("a_id", "b_id") in col_sets
    assert ("c_id", "d_id") in col_sets
    names = {uc.name for uc in ucs}
    assert len(names) == 2
    for uc in ucs:
        assert len(uc.name or "") <= 63


def test_composite_unique_order_matters_two_separate_constraints():
    """(x_id, y_id) and (y_id, x_id) are distinct groups — no silent merge."""

    class OrderMatters(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("x_id", "y_id"),
            ("y_id", "x_id"),
        )

        id: int | None = Field(default=None, primary_key=True)
        x_id: int
        y_id: int

    metadata = get_metadata()
    ucs = _unique_constraints(metadata.tables["ordermatters"])
    assert len(ucs) == 2


def test_build_sa_table_warns_on_invalid_composite_unique_group():
    """Malformed ferro_composite_uniques entries should warn, not fail silently."""
    from ferro.migrations.alembic import _build_sa_table

    md = sa.MetaData()
    schema = {
        "properties": {
            "id": {"type": "integer", "primary_key": True},
            "n": {"type": "integer"},
        },
        "required": ["id", "n"],
        "ferro_composite_uniques": [["n"]],
    }
    with pytest.warns(UserWarning, match="ferro_composite_uniques"):
        _build_sa_table(md, "warncomposite", schema, model_cls=None)
    assert "warncomposite" in md.tables


def test_single_column_composite_unique_raises_with_guidance():
    """A single-column group must error with guidance toward Field(unique=True)."""

    with pytest.raises(RuntimeError, match="at least two columns|Field\\(unique=True\\)"):

        class BadSingle(Model):
            __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("only_col",),
            )

            id: int | None = Field(default=None, primary_key=True)
            only_col: int
