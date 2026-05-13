"""U1: db_type / db_check propagate from Field() to FerroField and __ferro_schema__.

These tests cover the plumbing layer only -- no validation, no DDL emission.
Validation lives in U2 (metaclass), emitter dispatch lives in U3/U4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from ferro import Field, FerroField, Model


class FileFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"


def test_db_type_propagates_to_ferro_fields_and_schema():
    """Field(db_type="text") flows to ferro_fields and the enriched JSON schema."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text")

    metadata = Doc.ferro_fields["format"]
    assert metadata.db_type == "text"
    assert metadata.db_check is False

    props = Doc.__ferro_schema__["properties"]
    assert props["format"].get("db_type") == "text"
    assert "db_check" not in props["format"] or props["format"]["db_check"] is False


def test_db_check_propagates_to_ferro_fields_and_schema():
    """Field(db_type="text", db_check=True) flows both keys through to the schema."""

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: FileFormat = Field(db_type="text", db_check=True)

    metadata = Doc.ferro_fields["format"]
    assert metadata.db_type == "text"
    assert metadata.db_check is True

    props = Doc.__ferro_schema__["properties"]
    assert props["format"].get("db_type") == "text"
    assert props["format"].get("db_check") is True


def test_default_field_has_no_db_type_or_db_check():
    """Existing models (no db_type / db_check) must continue producing identical schemas.

    Regression guard for R13: backward compatibility.
    """

    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        email: str = Field(unique=True)

    metadata = User.ferro_fields["email"]
    assert metadata.db_type is None
    assert metadata.db_check is False

    props = User.__ferro_schema__["properties"]
    assert "db_type" not in props["email"]
    assert "db_check" not in props["email"]


def test_annotated_ferro_field_supports_db_type_and_db_check():
    """The Annotated[..., FerroField(db_type=...)] form is equivalent to Field()."""

    class Doc(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        format: Annotated[FileFormat, FerroField(db_type="text", db_check=True)]

    metadata = Doc.ferro_fields["format"]
    assert metadata.db_type == "text"
    assert metadata.db_check is True

    props = Doc.__ferro_schema__["properties"]
    assert props["format"].get("db_type") == "text"
    assert props["format"].get("db_check") is True
