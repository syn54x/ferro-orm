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
