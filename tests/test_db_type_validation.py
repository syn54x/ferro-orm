"""U2: strict validation of db_type / db_check at class-definition time.

Incoherent combinations raise TypeError when the model class is created,
before any query, emission, or migration runs.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum, IntEnum, StrEnum
from typing import Literal
from uuid import UUID

import pytest

from ferro import Field, Model


class FileFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


# ---------------------------------------------------------------------------
# Happy paths -- every canonical token on a compatible annotation
# ---------------------------------------------------------------------------


def test_text_on_strenum_is_accepted():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text")

    assert Doc.ferro_fields["format"].db_type == "text"


def test_text_on_str_is_accepted():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(db_type="text")

    assert Doc.ferro_fields["name"].db_type == "text"


def test_varchar_with_length_on_str_is_accepted():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str = Field(db_type="varchar(255)")

    assert Doc.ferro_fields["code"].db_type == "varchar(255)"


def test_bigint_on_int_is_accepted():
    class Counter(Model):
        id: int | None = Field(default=None, primary_key=True)
        value: int = Field(db_type="bigint")

    assert Counter.ferro_fields["value"].db_type == "bigint"


def test_smallint_on_intenum_is_accepted():
    class Task(Model):
        id: int | None = Field(default=None, primary_key=True)
        priority: Priority = Field(db_type="smallint")

    assert Task.ferro_fields["priority"].db_type == "smallint"


def test_uuid_on_uuid_field_is_accepted():
    class Record(Model):
        id: int | None = Field(default=None, primary_key=True)
        external_id: UUID = Field(db_type="uuid")

    assert Record.ferro_fields["external_id"].db_type == "uuid"


def test_text_on_uuid_field_is_accepted():
    """uuid -> text is the canonical portable-storage move."""

    class Record(Model):
        id: int | None = Field(default=None, primary_key=True)
        external_id: UUID = Field(db_type="text")

    assert Record.ferro_fields["external_id"].db_type == "text"


def test_timestamptz_on_datetime_is_accepted():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_at: dt.datetime = Field(db_type="timestamptz")

    assert Event.ferro_fields["occurred_at"].db_type == "timestamptz"


def test_date_on_date_is_accepted():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_on: dt.date = Field(db_type="date")

    assert Event.ferro_fields["occurred_on"].db_type == "date"


def test_time_on_time_is_accepted():
    class Event(Model):
        id: int | None = Field(default=None, primary_key=True)
        occurred_at: dt.time = Field(db_type="time")

    assert Event.ferro_fields["occurred_at"].db_type == "time"


# ---------------------------------------------------------------------------
# Error paths -- incoherent db_type / annotation combinations
# ---------------------------------------------------------------------------


def test_int_on_strenum_raises():
    with pytest.raises(TypeError, match="format.*db_type"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            format: FileFormat = Field(db_type="int")


def test_bigint_on_str_raises():
    with pytest.raises(TypeError, match="name.*db_type"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(db_type="bigint")


def test_uuid_on_str_raises():
    """Non-UUID Python field cannot declare db_type='uuid'."""
    with pytest.raises(TypeError, match="name.*db_type"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(db_type="uuid")


def test_unknown_token_raises():
    with pytest.raises(TypeError, match="banana"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(db_type="banana")


def test_text_on_int_raises():
    with pytest.raises(TypeError, match="value.*db_type"):

        class Counter(Model):
            id: int | None = Field(default=None, primary_key=True)
            value: int = Field(db_type="text")


def test_timestamp_on_int_raises():
    with pytest.raises(TypeError, match="created.*db_type"):

        class Row(Model):
            id: int | None = Field(default=None, primary_key=True)
            created: int = Field(db_type="timestamp")


def test_malformed_varchar_raises():
    with pytest.raises(TypeError, match="varchar"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            code: str = Field(db_type="varchar(notanumber)")


def test_varchar_zero_length_raises():
    with pytest.raises(TypeError, match="varchar"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            code: str = Field(db_type="varchar(0)")


# ---------------------------------------------------------------------------
# db_check validation
# ---------------------------------------------------------------------------


def test_db_check_without_db_type_on_enum_raises():
    """Native enum storage already validates values; db_check is redundant."""
    with pytest.raises(TypeError, match="db_check.*db_type"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            format: FileFormat = Field(db_check=True)


def test_db_check_on_plain_int_raises():
    """db_check is only for closed-domain types (Enum / Literal)."""
    with pytest.raises(TypeError, match="db_check"):

        class Counter(Model):
            id: int | None = Field(default=None, primary_key=True)
            value: int = Field(db_type="bigint", db_check=True)


def test_db_check_on_plain_str_raises():
    with pytest.raises(TypeError, match="db_check"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(db_type="text", db_check=True)


def test_db_check_with_db_type_on_strenum_is_accepted():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text", db_check=True)

    metadata = Doc.ferro_fields["format"]
    assert metadata.db_type == "text"
    assert metadata.db_check is True


def test_db_check_with_db_type_on_intenum_is_accepted():
    class Task(Model):
        id: int | None = Field(default=None, primary_key=True)
        priority: Priority = Field(db_type="smallint", db_check=True)

    assert Task.ferro_fields["priority"].db_check is True


# ---------------------------------------------------------------------------
# Optional / nullable annotations should not change validation
# ---------------------------------------------------------------------------


def test_optional_annotation_does_not_block_validation():
    """`StrEnum | None` is still a StrEnum-family field for db_type purposes."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat | None = Field(default=None, db_type="text")

    assert Doc.ferro_fields["format"].db_type == "text"


def test_optional_annotation_still_rejects_incoherent_db_type():
    with pytest.raises(TypeError, match="format.*db_type"):

        class Doc(Model):
            id: int | None = Field(default=None, primary_key=True)
            format: FileFormat | None = Field(default=None, db_type="int")


# ---------------------------------------------------------------------------
# Backward compat: existing models with no db_type/db_check still import
# ---------------------------------------------------------------------------


def test_existing_field_usage_unaffected():
    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(unique=True, index=True)
        format: FileFormat = FileFormat.PDF  # no ferro metadata at all

    assert User.ferro_fields["email"].db_type is None
    assert User.ferro_fields["email"].db_check is False
    assert "format" not in User.ferro_fields


# Literal[...] db_check support is deferred per the plan; document the
# expected behavior so the regression check is explicit when U2 ships.
@pytest.mark.xfail(reason="Literal[...] db_check deferred per plan U2")
def test_db_check_on_literal_is_accepted():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        status: Literal["draft", "live"] = Field(db_type="text", db_check=True)

    assert Doc.ferro_fields["status"].db_check is True
