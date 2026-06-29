"""U5: cross-emitter parity for every canonical db_type token.

For every ``(canonical_token, dialect)`` pair, the Alembic-rendered CREATE TABLE
SQL and the Rust-emitter-rendered CREATE TABLE SQL must agree on the column's
SQL type keyword. For closed-domain combos with ``db_check=True``, the
``ck_<table>_<col>`` constraint name must match byte-for-byte.

If this test fails, one emitter has drifted from the other. See AGENTS.md I-1
and ``docs/solutions/patterns/cross-emitter-ddl-parity.md``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from enum import IntEnum, StrEnum
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from ferro import Field, Model, clear_registry, reset_engine
from ferro._core import _render_create_table_sql_for_test
from ferro.ir.compiler import compile_schema_ir_payload
from ferro.migrations import get_metadata
from ferro.schema_metadata import build_model_schema


def _render_create_table_via_ir(
    name: str, model_cls: type[Model], dialect_name: str
) -> tuple[str, list[str]]:
    """Compile ``model_cls`` to a SchemaIR payload and render the runtime CREATE
    TABLE through the shared emitter (the same path the runtime uses)."""
    schema = build_model_schema(model_cls)
    payload = compile_schema_ir_payload(name, schema)
    return _render_create_table_sql_for_test(name, json.dumps(payload), dialect_name)


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import (
        _JOIN_TABLE_REGISTRY,
        _MODEL_REGISTRY_PY,
        _PENDING_RELATIONS,
    )

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()


# ---------------------------------------------------------------------------
# Token catalog: (token, python_annotation, expected_keywords_per_dialect)
# Keywords are the case-insensitive substrings that must appear in BOTH the
# Alembic-compiled SQL and the Rust-emitted SQL for the named column.
# ---------------------------------------------------------------------------


class _Format(StrEnum):
    PDF = "pdf"
    JSON = "json"


class _Priority(IntEnum):
    LOW = 1
    HIGH = 2


_TOKEN_CASES = [
    pytest.param("text", str, {"postgres": "TEXT", "sqlite": "TEXT"}, id="text-str"),
    pytest.param(
        "text", _Format, {"postgres": "TEXT", "sqlite": "TEXT"}, id="text-strenum"
    ),
    pytest.param(
        "varchar(255)",
        str,
        {"postgres": "VARCHAR(255)", "sqlite": "VARCHAR(255)"},
        id="varchar-255",
    ),
    pytest.param("int", int, {"postgres": "INTEGER", "sqlite": "INTEGER"}, id="int"),
    pytest.param(
        "smallint",
        int,
        {"postgres": "SMALLINT", "sqlite": "SMALLINT"},
        id="smallint",
    ),
    pytest.param(
        "bigint",
        int,
        {"postgres": "BIGINT", "sqlite": "BIGINT"},
        id="bigint",
    ),
    pytest.param(
        "uuid", UUID, {"postgres": "UUID", "sqlite": "CHAR(32)"}, id="uuid"
    ),
    pytest.param(
        "timestamp",
        dt.datetime,
        {"postgres": "TIMESTAMP", "sqlite": "DATETIME"},
        id="timestamp",
    ),
    pytest.param(
        "timestamptz",
        dt.datetime,
        {"postgres": "TIMESTAMP WITH TIME ZONE", "sqlite": "DATETIME"},
        id="timestamptz",
    ),
    pytest.param("date", dt.date, {"postgres": "DATE", "sqlite": "DATE"}, id="date"),
    pytest.param("time", dt.time, {"postgres": "TIME", "sqlite": "TIME"}, id="time"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_COL_RE_TEMPLATE = r'"?{col}"?\s+([A-Z][A-Z0-9_()\s]*?)(?:\s+(?:NOT\s+NULL|NULL|PRIMARY|DEFAULT|UNIQUE|REFERENCES|\)|,))'


def _extract_col_type(sql: str, col_name: str) -> str:
    """Pull the column-type substring for ``col_name`` out of a CREATE TABLE.

    Returns the uppercased type text up to the first trailing column attribute
    (NOT NULL, comma, closing paren, etc.). Whitespace is normalized.
    """
    pattern = re.compile(_COL_RE_TEMPLATE.format(col=re.escape(col_name)), re.IGNORECASE)
    match = pattern.search(sql)
    if match is None:
        raise AssertionError(
            f"Column {col_name!r} not found in SQL fragment:\n{sql}"
        )
    return " ".join(match.group(1).upper().split())


def _render_alembic_sql(model_cls: type[Model], dialect_name: str) -> str:
    metadata = get_metadata()
    table = metadata.tables[model_cls.__name__.lower()]
    dialect = (
        postgresql.dialect() if dialect_name == "postgres" else sqlite.dialect()
    )
    return str(CreateTable(table).compile(dialect=dialect))


def _render_rust_sql(model_cls: type[Model], dialect_name: str) -> str:
    table_sql, _ = _render_create_table_via_ir(
        model_cls.__name__, model_cls, dialect_name
    )
    return table_sql


# ---------------------------------------------------------------------------
# Parity tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
@pytest.mark.parametrize("token,annotation,expected", _TOKEN_CASES)
def test_column_type_parity_across_emitters(
    token: str, annotation: type, expected: dict[str, str], dialect: str
):
    """Both emitters render the same SQL keyword for every canonical token."""

    # Dynamically build a one-column model with the given annotation + db_type.
    namespace = {
        "__annotations__": {"id": int | None, "x": annotation},
        "id": Field(default=None, primary_key=True),
        "x": Field(db_type=token),
    }
    Model_x = type("ParityModel", (Model,), namespace)

    alembic_sql = _render_alembic_sql(Model_x, dialect)
    rust_sql = _render_rust_sql(Model_x, dialect)

    alembic_type = _extract_col_type(alembic_sql, "x")
    rust_type = _extract_col_type(rust_sql, "x")

    expected_keyword = expected[dialect]
    assert expected_keyword in alembic_type, (
        f"Alembic ({dialect}) missing {expected_keyword!r} for db_type={token!r}; "
        f"got {alembic_type!r}\nFull SQL: {alembic_sql}"
    )
    assert expected_keyword in rust_type, (
        f"Rust ({dialect}) missing {expected_keyword!r} for db_type={token!r}; "
        f"got {rust_type!r}\nFull SQL: {rust_sql}"
    )


# ---------------------------------------------------------------------------
# db_check constraint name parity (Postgres only -- SQLite elides db_check)
# ---------------------------------------------------------------------------


def test_db_check_constraint_name_parity_strenum():
    """AE2: ck_<table>_<col> is byte-identical between emitters."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _Format = Field(db_type="text", db_check=True)

    table = get_metadata().tables["doc"]
    sa_checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert len(sa_checks) == 1
    sa_name = sa_checks[0].name

    _, rust_post = _render_create_table_via_ir("Doc", Doc, "postgres")
    assert any("ck_doc_format" in s for s in rust_post), (
        f"Rust emitter missing ck_doc_format in {rust_post!r}"
    )
    assert sa_name == "ck_doc_format" == "ck_doc_format"
    # Cross-emitter name parity
    assert any(sa_name in s for s in rust_post)


def test_db_check_constraint_name_parity_intenum():
    class Task(Model):
        id: int | None = Field(default=None, primary_key=True)
        priority: _Priority = Field(db_type="smallint", db_check=True)

    table = get_metadata().tables["task"]
    sa_checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    sa_name = sa_checks[0].name
    assert sa_name == "ck_task_priority"

    _, rust_post = _render_create_table_via_ir("Task", Task, "postgres")
    assert any(sa_name in s for s in rust_post)


def test_db_check_elided_on_sqlite_in_both_emitters():
    """SQLite's ADD CONSTRAINT limitation: db_check is Postgres-only at runtime.

    The Alembic side will still attach a CheckConstraint to the SA Table (SA
    can render an inline CHECK for SQLite), but the Rust runtime emitter
    elides the ALTER TABLE entirely. This test pins the asymmetry so it
    doesn't regress silently.
    """

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _Format = Field(db_type="text", db_check=True)

    _, rust_post = _render_create_table_via_ir("Doc", Doc, "sqlite")
    assert all("CONSTRAINT" not in s.upper() for s in rust_post)


# ---------------------------------------------------------------------------
# R13: a model with no db_type/db_check produces identical column-type output
# to the pre-feature baseline (this is the regression guard against the new
# branches accidentally rerouting default models).
# ---------------------------------------------------------------------------


def test_no_db_type_keeps_default_emitter_behavior():
    class Counter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int

    alembic_sql = _render_alembic_sql(Counter, "postgres")
    rust_sql = _render_rust_sql(Counter, "postgres")

    # Default int -> INTEGER on both sides, no BIGINT or SMALLINT leakage.
    assert "INTEGER" in _extract_col_type(alembic_sql, "value")
    assert "INTEGER" in _extract_col_type(rust_sql, "value")
    assert "BIGINT" not in _extract_col_type(rust_sql, "value")
