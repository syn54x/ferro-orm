"""Composite (non-unique) multi-column indexes declared on Ferro models."""

from typing import ClassVar

import pytest
import sqlalchemy as sa

from ferro import (
    BackRef,
    Field,
    ManyToMany,
    Model,
    Relation,
    clear_registry,
    connect,
    reset_engine,
)
from ferro.migrations import get_metadata

pytestmark = pytest.mark.backend_matrix


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


def _indexes(table: sa.Table) -> list[sa.Index]:
    return list(table.indexes)


# === Group A: declarative-API validation ===


def test_composite_index_unknown_column_raises():
    """A1: unknown column -> RuntimeError at class-definition time."""
    with pytest.raises(RuntimeError, match="unknown column"):

        class BadIdx(Model):
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("alpha_id", "nonexistent"),
            )
            id: int | None = Field(default=None, primary_key=True)
            alpha_id: int


def test_single_column_composite_index_raises_with_guidance():
    """A2: single-column inner tuple -> RuntimeError pointing to Field(index=True)."""
    with pytest.raises(
        RuntimeError, match="at least two columns|Field\\(index=True\\)"
    ):

        class BadSingle(Model):
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("only_col",),
            )
            id: int | None = Field(default=None, primary_key=True)
            only_col: int


def test_empty_inner_tuple_raises():
    """A3: empty inner tuple -> RuntimeError."""
    with pytest.raises(RuntimeError, match="at least two columns"):

        class BadEmpty(Model):
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                (),
            )
            id: int | None = Field(default=None, primary_key=True)
            a: int
            b: int


def test_non_string_column_entry_raises():
    """A4: non-string entry -> RuntimeError."""
    with pytest.raises(RuntimeError, match="must be a non-empty str"):

        class BadType(Model):
            __ferro_composite_indexes__: ClassVar = (("col_a", 42),)
            id: int | None = Field(default=None, primary_key=True)
            col_a: int


def test_outer_not_a_tuple_raises():
    """A5: outer not a tuple -> RuntimeError."""
    with pytest.raises(RuntimeError, match="must be a tuple of tuples"):

        class BadOuter(Model):
            __ferro_composite_indexes__: ClassVar = "not_a_tuple"
            id: int | None = Field(default=None, primary_key=True)
            a: int


def test_inner_not_a_tuple_raises():
    """A6: inner element not a tuple -> RuntimeError."""
    with pytest.raises(RuntimeError, match="must be a tuple of str"):

        class BadInner(Model):
            __ferro_composite_indexes__: ClassVar = (("a", "b"), ["c", "d"])
            id: int | None = Field(default=None, primary_key=True)
            a: int
            b: int
            c: int
            d: int


def test_empty_default_is_noop():
    """A7: model with no declaration -> no ferro_composite_indexes key in schema."""

    class NoIndexes(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    schema = NoIndexes.__ferro_schema__
    assert "ferro_composite_indexes" not in schema


def test_duplicate_ordered_tuple_dedupes_silently():
    """A8: identical ordered tuples -> one index, no warning."""

    class Dup(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b"),
            ("a", "b"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int

    schema = Dup.__ferro_schema__
    assert schema["ferro_composite_indexes"] == [["a", "b"]]


def test_three_column_composite_index():
    """A9: 3-column group materializes with declared order."""

    class Triple(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b", "c"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int
        c: int

    schema = Triple.__ferro_schema__
    assert schema["ferro_composite_indexes"] == [["a", "b", "c"]]


def test_schema_json_uses_lists_not_tuples():
    """B11: wire format is list[list[str]]; JSON-roundtrip safe."""
    import json

    class WireFmt(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int

    schema = WireFmt.__ferro_schema__
    payload = schema["ferro_composite_indexes"]
    assert isinstance(payload, list)
    assert all(isinstance(g, list) for g in payload)
    json.loads(json.dumps(schema))


# === Group B (subset): overlap handling ===


def test_overlap_with_unique_warns_and_drops():
    """B6: same ordered tuple in both kinds -> UserWarning, only unique materializes."""

    with pytest.warns(UserWarning, match="duplicates an existing __ferro_composite_uniques__"):

        class Dup(Model):
            __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("a", "b"),
            )
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("a", "b"),
            )
            id: int | None = Field(default=None, primary_key=True)
            a: int
            b: int

    schema = Dup.__ferro_schema__
    assert "ferro_composite_indexes" not in schema
    assert schema["ferro_composite_uniques"] == [["a", "b"]]


def test_overlap_reordered_does_not_warn():
    """B7: ('a','b') unique + ('b','a') index -> both materialize, no warning."""
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error", UserWarning)

        class Reordered(Model):
            __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("a", "b"),
            )
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("b", "a"),
            )
            id: int | None = Field(default=None, primary_key=True)
            a: int
            b: int

    schema = Reordered.__ferro_schema__
    assert schema["ferro_composite_uniques"] == [["a", "b"]]
    assert schema["ferro_composite_indexes"] == [["b", "a"]]


def test_overlap_with_unique_partial_match_does_not_warn():
    """B8: ('a','b','c') unique + ('a','b') index -> no warning (different lengths)."""
    import warnings as warnings_mod

    with warnings_mod.catch_warnings():
        warnings_mod.simplefilter("error", UserWarning)

        class Partial(Model):
            __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("a", "b", "c"),
            )
            __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
                ("a", "b"),
            )
            id: int | None = Field(default=None, primary_key=True)
            a: int
            b: int
            c: int

    schema = Partial.__ferro_schema__
    assert schema["ferro_composite_uniques"] == [["a", "b", "c"]]
    assert schema["ferro_composite_indexes"] == [["a", "b"]]


# === Group B: schema-bridge correctness (Alembic) ===


def test_alembic_metadata_has_index_objects():
    """B1: get_metadata() exposes a non-unique sa.Index for the composite group."""

    class IdxModel(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int

    metadata = get_metadata()
    table = metadata.tables["idxmodel"]
    indexes = _indexes(table)
    composite = [i for i in indexes if {c.key for c in i.columns} == {"a", "b"}]
    assert len(composite) == 1
    assert composite[0].unique is False


def test_alembic_metadata_index_column_order_matches_declaration():
    """B2: sa.Index column order equals declared tuple order."""

    class OrderedIdx(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("y", "x"),
        )
        id: int | None = Field(default=None, primary_key=True)
        x: int
        y: int

    metadata = get_metadata()
    idx = next(
        i for i in metadata.tables["orderedidx"].indexes
        if {c.key for c in i.columns} == {"x", "y"}
    )
    assert [c.key for c in idx.columns] == ["y", "x"]


def test_composite_index_multiple_groups():
    """B3: two disjoint groups -> two distinct non-unique indexes."""

    class MultiIdx(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b"),
            ("c", "d"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int
        c: int
        d: int

    metadata = get_metadata()
    indexes = [i for i in metadata.tables["multiidx"].indexes if not i.unique]
    col_sets = {tuple(c.key for c in i.columns) for i in indexes}
    assert ("a", "b") in col_sets
    assert ("c", "d") in col_sets
    names = {i.name for i in indexes}
    assert len(names) == len(indexes)


def test_composite_index_order_matters_two_separate_indexes():
    """B4: ('x','y') and ('y','x') -> two separate indexes."""

    class OrderMatters(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("x", "y"),
            ("y", "x"),
        )
        id: int | None = Field(default=None, primary_key=True)
        x: int
        y: int

    metadata = get_metadata()
    indexes = [
        i for i in metadata.tables["ordermatters"].indexes
        if not i.unique and {c.key for c in i.columns} == {"x", "y"}
    ]
    assert len(indexes) == 2


def test_uniques_and_indexes_coexist():
    """B5: disjoint groups in both kinds materialize side-by-side."""

    class Both(Model):
        __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("u1", "u2"),
        )
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("i1", "i2"),
        )
        id: int | None = Field(default=None, primary_key=True)
        u1: int
        u2: int
        i1: int
        i2: int

    metadata = get_metadata()
    table = metadata.tables["both"]
    ucs = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    idxs = [i for i in table.indexes if not i.unique]
    assert any({c.key for c in uc.columns} == {"u1", "u2"} for uc in ucs)
    assert any({c.key for c in i.columns} == {"i1", "i2"} for i in idxs)
    all_names = {uc.name for uc in ucs} | {i.name for i in idxs}
    assert len(all_names) == len(ucs) + len(idxs)


def test_coexists_with_single_column_field_index():
    """B9: Field(index=True) on a column AND a composite over that column -> both materialize."""

    class WithSingle(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("col_a", "col_b"),
        )
        id: int | None = Field(default=None, primary_key=True)
        col_a: int = Field(index=True)
        col_b: int

    metadata = get_metadata()
    table = metadata.tables["withsingle"]
    column_idxs = [
        i for i in table.indexes
        if {c.key for c in i.columns} == {"col_a"}
    ]
    composite_idxs = [
        i for i in table.indexes
        if {c.key for c in i.columns} == {"col_a", "col_b"}
    ]
    assert len(column_idxs) >= 1
    assert len(composite_idxs) == 1


def test_build_sa_table_warns_on_invalid_composite_index_group():
    """B10: hand-edited schema with single-element group -> UserWarning, table still builds."""
    from ferro.migrations.alembic import _build_sa_table

    md = sa.MetaData()
    schema = {
        "properties": {
            "id": {"type": "integer", "primary_key": True},
            "n": {"type": "integer"},
        },
        "required": ["id", "n"],
        "ferro_composite_indexes": [["n"]],
    }
    with pytest.warns(UserWarning, match="ferro_composite_indexes"):
        _build_sa_table(md, "warnidx", schema, model_cls=None)
    assert "warnidx" in md.tables


def test_composite_index_name_python_matches_naming_convention():
    """B12: Python-side name follows idx_<table>_<cols> with _idx truncation suffix."""

    class ShortName(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("a", "b"),
        )
        id: int | None = Field(default=None, primary_key=True)
        a: int
        b: int

    metadata = get_metadata()
    idx = next(i for i in metadata.tables["shortname"].indexes if not i.unique)
    assert idx.name == "idx_shortname_a_b"

    long_col_a = "very_long_column_name_alpha_for_idx_truncation_test"
    long_col_b = "very_long_column_name_beta_for_idx_truncation_test"

    class VeryLongCompositeIndexModelNameForTruncation(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            (long_col_a, long_col_b),
        )
        id: int | None = Field(default=None, primary_key=True)
        very_long_column_name_alpha_for_idx_truncation_test: int
        very_long_column_name_beta_for_idx_truncation_test: int

    table_lower = "verylongcompositeindexmodelnamefortruncation"
    metadata = get_metadata()
    long_idx = next(
        i for i in metadata.tables[table_lower].indexes if not i.unique
    )
    assert len(long_idx.name) == 63
    assert long_idx.name.endswith("_idx")


# === Group C (subset): SQLite live catalog ===


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_composite_index_exists_in_sqlite(db_url):
    """C1: after auto_migrate, sqlite_master has the index without UNIQUE."""
    import sqlite3

    class IdxRow(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
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
        "AND tbl_name='idxrow' AND sql IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    composite = [
        r for r in rows
        if r[1] and "alpha_id" in r[1] and "beta_id" in r[1]
    ]
    assert composite, f"expected composite index on idxrow, got: {rows}"
    name, sql = composite[0]
    assert name == "idx_idxrow_alpha_id_beta_id"
    assert "UNIQUE" not in sql.upper()


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_composite_index_column_order_in_sqlite_indexdef(db_url):
    """C3: SQLite index SQL preserves declared column order."""
    import sqlite3

    class OrderRow(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("y_id", "x_id"),
        )
        id: int | None = Field(default=None, primary_key=True)
        x_id: int
        y_id: int

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='orderrow' "
        "AND sql IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    pos_y = sql.index("y_id")
    pos_x = sql.index("x_id")
    assert pos_y < pos_x, f"expected y_id before x_id in: {sql}"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_composite_index_truncated_name_matches_alembic_and_sqlite(db_url):
    """C5: long composite-index names truncate identically in Alembic and Rust."""
    import sqlite3

    long_a = "very_long_column_name_alpha_for_idx_truncation_test"
    long_b = "very_long_column_name_beta_for_idx_truncation_test"

    class VeryLongCompositeIndexModelNameForTruncation(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            (long_a, long_b),
        )
        id: int | None = Field(default=None, primary_key=True)
        very_long_column_name_alpha_for_idx_truncation_test: int
        very_long_column_name_beta_for_idx_truncation_test: int

    table = "verylongcompositeindexmodelnamefortruncation"
    metadata = get_metadata()
    idx = next(i for i in metadata.tables[table].indexes if not i.unique)
    expected = idx.name
    assert len(expected) == 63
    assert expected.endswith("_idx")

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    conn.close()

    names = [r[0] for r in rows]
    assert expected in names, f"expected {expected!r} in {names}"


# === Group C (subset): Postgres live catalog ===


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_composite_index_exists_in_postgres(db_url, postgres_base_url, db_schema_name):
    """C2: after auto_migrate, pg_indexes has the index without UNIQUE."""
    import psycopg

    class PgIdxRow(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("alpha_id", "beta_id"),
        )
        id: int | None = Field(default=None, primary_key=True)
        alpha_id: int
        beta_id: int

    await connect(db_url, auto_migrate=True)

    expected = "idx_pgidxrow_alpha_id_beta_id"
    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s AND indexname = %s",
            (db_schema_name, "pgidxrow", expected),
        ).fetchone()
    assert row is not None
    indexdef = row[0]
    assert "UNIQUE" not in indexdef.upper()


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_composite_index_column_order_in_postgres_indexdef(
    db_url, postgres_base_url, db_schema_name
):
    """C4: pg_indexes indexdef preserves declared column order."""
    import psycopg

    class PgOrder(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("y_id", "x_id"),
        )
        id: int | None = Field(default=None, primary_key=True)
        x_id: int
        y_id: int

    await connect(db_url, auto_migrate=True)

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s AND indexname = %s",
            (db_schema_name, "pgorder", "idx_pgorder_y_id_x_id"),
        ).fetchone()
    assert row is not None
    indexdef = row[0]
    pos_y = indexdef.index("y_id")
    pos_x = indexdef.index("x_id")
    assert pos_y < pos_x


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_composite_index_truncated_name_matches_postgres_catalog(
    db_url, postgres_base_url, db_schema_name
):
    """C6: long composite-index names match between Alembic and Postgres."""
    import psycopg

    long_a = "very_long_column_name_alpha_for_idx_truncation_test"
    long_b = "very_long_column_name_beta_for_idx_truncation_test"

    class VeryLongPgCompositeIndexModel(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            (long_a, long_b),
        )
        id: int | None = Field(default=None, primary_key=True)
        very_long_column_name_alpha_for_idx_truncation_test: int
        very_long_column_name_beta_for_idx_truncation_test: int

    table = "verylongpgcompositeindexmodel"
    metadata = get_metadata()
    expected = next(i for i in metadata.tables[table].indexes if not i.unique).name
    assert len(expected) <= 63

    await connect(db_url, auto_migrate=True)

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s AND indexname = %s",
            (db_schema_name, table, expected),
        ).fetchone()
    assert row is not None
    assert row[0] == expected


# === Group C (subset): mixed-type columns + autogen idempotence ===


@pytest.mark.asyncio
async def test_composite_index_works_with_uuid_columns(db_url):
    """C7: composite index over two UUID columns materializes."""
    import uuid

    class UuidComposite(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("tenant_id", "user_id"),
        )
        id: uuid.UUID | None = Field(default=None, primary_key=True)
        tenant_id: uuid.UUID
        user_id: uuid.UUID

    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()
    idxs = [i for i in metadata.tables["uuidcomposite"].indexes if not i.unique]
    assert any(
        {c.key for c in i.columns} == {"tenant_id", "user_id"}
        for i in idxs
    )


@pytest.mark.asyncio
async def test_composite_index_works_with_enum_column(db_url):
    """C8: composite index mixing FK and enum column materializes."""
    import enum

    class Role(str, enum.Enum):
        admin = "admin"
        member = "member"

    class EnumComposite(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("user_id", "role"),
        )
        id: int | None = Field(default=None, primary_key=True)
        user_id: int
        role: Role

    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()
    idxs = [i for i in metadata.tables["enumcomposite"].indexes if not i.unique]
    assert any(
        {c.key for c in i.columns} == {"user_id", "role"}
        for i in idxs
    )


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_autogen_idempotent_after_first_apply(db_url):
    """C9: re-running autogen comparison after migrate produces no diff for composite indexes.

    Note: composite-uniques (`uq_*`) autogen drift is a pre-existing quirk
    (UniqueConstraint vs. CREATE UNIQUE INDEX reconciliation on SQLite) that
    is independent of this feature. We assert only that our new
    `idx_*` composite indexes do not introduce additional drift.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    class IdxStable(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("c", "d"),
        )
        id: int | None = Field(default=None, primary_key=True)
        c: int
        d: int

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    sync_url = f"sqlite:///{db_path}"
    engine = create_engine(sync_url)
    metadata = get_metadata()

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, metadata)

    relevant = [d for d in diff if "idx_idxstable" in str(d).lower()]
    assert relevant == [], f"unexpected autogen drift on composite indexes: {relevant}"


# === Group F: common-use-case smoke tests ===


@pytest.mark.asyncio
async def test_use_case_m2m_reverse_query(db_url):
    """F1: actually query an M2M reverse direction; reverse index is queryable."""

    class TagF1(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        articles: Relation[list["ArticleF1"]] = ManyToMany(related_name="tags")

    class ArticleF1(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list["TagF1"]] = BackRef()

    await connect(db_url, auto_migrate=True)

    python_tag = await TagF1.create(name="python")
    rust_tag = await TagF1.create(name="rust")
    article_a = await ArticleF1.create(title="A")
    article_b = await ArticleF1.create(title="B")

    await python_tag.articles.add(article_a)
    await python_tag.articles.add(article_b)
    await rust_tag.articles.add(article_a)

    fetched_a = await ArticleF1.get(article_a.id)
    a_tags = await fetched_a.tags.all()
    assert {t.name for t in a_tags} == {"python", "rust"}

    fetched_b = await ArticleF1.get(article_b.id)
    b_tags = await fetched_b.tags.all()
    assert {t.name for t in b_tags} == {"python"}


@pytest.mark.asyncio
async def test_use_case_polymorphic_lookup_index(db_url):
    """F2: composite index on (string, int) mixed-type columns materializes."""
    import sqlite3

    class CommentF2(Model):
        __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = (
            ("content_type", "object_id"),
        )
        id: int | None = Field(default=None, primary_key=True)
        content_type: str
        object_id: int
        body: str

    await connect(db_url, auto_migrate=True)

    metadata = get_metadata()
    idxs = [i for i in metadata.tables["commentf2"].indexes if not i.unique]
    composite = [
        i for i in idxs
        if [c.key for c in i.columns] == ["content_type", "object_id"]
    ]
    assert len(composite) == 1

    if "sqlite" in db_url:
        db_path = db_url.replace("sqlite:", "").split("?")[0]
        conn = sqlite3.connect(db_path)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='commentf2' "
            "AND name='idx_commentf2_content_type_object_id'"
        ).fetchone()
        conn.close()
        assert sql is not None
