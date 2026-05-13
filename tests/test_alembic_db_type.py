"""U3: Alembic emitter honors db_type / db_check.

The SA MetaData built by ``ferro.migrations.get_metadata()`` must reflect the
canonical db_type token and emit a CheckConstraint named ``ck_<table>_<col>``
when db_check is set. Default-behavior models (no db_type) must produce the
same MetaData shape as before this unit.
"""

from __future__ import annotations

import datetime as dt
from enum import IntEnum, StrEnum
from uuid import UUID

import pytest
import sqlalchemy as sa

from ferro import Field, Model, clear_registry, reset_engine
from ferro.migrations import get_metadata


class FileFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


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


# ---------------------------------------------------------------------------
# Column-type dispatch
# ---------------------------------------------------------------------------


def test_text_on_strenum_renders_text_not_enum():
    """AE1: ``Field(db_type='text')`` on a StrEnum field is a TEXT column,
    not a named SA Enum (so Alembic never emits CREATE TYPE)."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text")

    table = get_metadata().tables["doc"]
    col = table.c.format

    assert not isinstance(col.type, sa.Enum)
    # sa.Text / sa.String both compile to TEXT/VARCHAR; assert the string family
    assert isinstance(col.type, (sa.Text, sa.String))


def test_varchar_with_length_on_str_renders_string_n():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str = Field(db_type="varchar(255)")

    col = get_metadata().tables["doc"].c.code
    assert isinstance(col.type, sa.String)
    assert col.type.length == 255


def test_bigint_on_int_renders_big_integer():
    class Counter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int = Field(db_type="bigint")

    col = get_metadata().tables["counter"].c.value
    assert isinstance(col.type, sa.BigInteger)


def test_smallint_on_intenum_renders_small_integer():
    class Task(Model):
        id: int | None = Field(default=None, primary_key=True)
        priority: Priority = Field(db_type="smallint")

    col = get_metadata().tables["task"].c.priority
    assert isinstance(col.type, sa.SmallInteger)
    assert not isinstance(col.type, sa.Enum)


def test_uuid_on_uuid_renders_sa_uuid():
    class Record(Model):
        id: int | None = Field(default=None, primary_key=True)
        ext_id: UUID = Field(db_type="uuid")

    col = get_metadata().tables["record"].c.ext_id
    # SA >= 2 provides sa.Uuid; older SA falls back to String(36)
    assert isinstance(col.type, (sa.Uuid, sa.String)) if hasattr(
        sa, "Uuid"
    ) else isinstance(col.type, sa.String)


def test_text_on_uuid_renders_string_family():
    class Record(Model):
        id: int | None = Field(default=None, primary_key=True)
        ext_id: UUID = Field(db_type="text")

    col = get_metadata().tables["record"].c.ext_id
    assert isinstance(col.type, (sa.Text, sa.String))
    assert not (hasattr(sa, "Uuid") and isinstance(col.type, sa.Uuid))


def test_timestamptz_on_datetime_renders_datetime_with_timezone():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_at: dt.datetime = Field(db_type="timestamptz")

    col = get_metadata().tables["event"].c.occurred_at
    assert isinstance(col.type, sa.DateTime)
    assert col.type.timezone is True


def test_timestamp_on_datetime_renders_datetime_without_timezone():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_at: dt.datetime = Field(db_type="timestamp")

    col = get_metadata().tables["event"].c.occurred_at
    assert isinstance(col.type, sa.DateTime)
    assert col.type.timezone is False


def test_date_on_date_renders_sa_date():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_on: dt.date = Field(db_type="date")

    col = get_metadata().tables["event"].c.occurred_on
    assert isinstance(col.type, sa.Date)


def test_time_on_time_renders_sa_time():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_at: dt.time = Field(db_type="time")

    col = get_metadata().tables["event"].c.occurred_at
    assert isinstance(col.type, sa.Time)


# ---------------------------------------------------------------------------
# db_check constraint
# ---------------------------------------------------------------------------


def _check_constraints(table: sa.Table) -> list[sa.CheckConstraint]:
    return [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]


def test_db_check_on_strenum_emits_named_check_constraint():
    """AE2: db_check=True attaches a CheckConstraint named ck_<table>_<col>
    containing every enum value in an IN(...) clause."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text", db_check=True)

    table = get_metadata().tables["doc"]
    checks = _check_constraints(table)
    assert len(checks) == 1
    ck = checks[0]
    assert ck.name == "ck_doc_format"

    sqltext = str(ck.sqltext)
    assert "format" in sqltext
    assert "'pdf'" in sqltext
    assert "'json'" in sqltext
    assert " IN " in sqltext.upper()


def test_db_check_on_intenum_emits_check_with_integer_values():
    class Task(Model):
        id: int | None = Field(default=None, primary_key=True)
        priority: Priority = Field(db_type="smallint", db_check=True)

    table = get_metadata().tables["task"]
    checks = _check_constraints(table)
    assert len(checks) == 1
    ck = checks[0]
    assert ck.name == "ck_task_priority"
    sqltext = str(ck.sqltext)
    # Integer values are unquoted
    assert "1" in sqltext and "2" in sqltext
    assert "'1'" not in sqltext


def test_db_check_without_db_check_kwarg_emits_no_constraint():
    """db_check defaults to False -- no CheckConstraint added."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text")

    checks = _check_constraints(get_metadata().tables["doc"])
    assert checks == []


# ---------------------------------------------------------------------------
# Backward compat / R13: existing models unchanged
# ---------------------------------------------------------------------------


def test_default_enum_still_renders_named_sa_enum():
    """A model that does not set db_type continues to map StrEnum -> sa.Enum
    with the existing name convention (R13)."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat

    col = get_metadata().tables["doc"].c.format
    assert isinstance(col.type, sa.Enum)
    assert col.type.name == "fileformat"


def test_default_int_still_renders_integer():
    class Counter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int

    col = get_metadata().tables["counter"].c.value
    assert isinstance(col.type, sa.Integer)
    assert not isinstance(col.type, sa.BigInteger)


# ---------------------------------------------------------------------------
# Naming convention exposes the new "ck" key
# ---------------------------------------------------------------------------


def test_naming_convention_includes_ck_key():
    """Cross-emitter parity (AGENTS.md I-1) -- the ck_<table>_<col> name must
    be governed by the same naming convention as idx_/uq_."""
    from ferro.migrations.alembic import _FERRO_NAMING_CONVENTION

    assert "ck" in _FERRO_NAMING_CONVENTION
    assert _FERRO_NAMING_CONVENTION["ck"] == "ck_%(table_name)s_%(column_0_name)s"
