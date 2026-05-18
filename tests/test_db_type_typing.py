"""Typing surface for db_type: DbTypeToken, DbType, varchar()."""

from __future__ import annotations

import pytest

from ferro import DbTypeToken, Field, Model, varchar


def test_varchar_helper_builds_canonical_token():
    assert varchar(255) == "varchar(255)"


def test_varchar_rejects_non_positive_length():
    with pytest.raises(ValueError, match="positive"):
        varchar(0)


def test_field_accepts_db_type_token_literal():
    token: DbTypeToken = "text"

    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(db_type=token)

    assert Doc.ferro_fields["name"].db_type == "text"


def test_field_accepts_varchar_helper():
    class Doc(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str = Field(db_type=varchar(64))

    assert Doc.ferro_fields["code"].db_type == "varchar(64)"
