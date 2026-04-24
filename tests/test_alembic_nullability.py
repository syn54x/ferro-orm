"""Alembic bridge: Column.nullable from infer / explicit overrides."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, TypeAliasType, Union

import pytest
from pydantic import ValidationError

from ferro import FerroField, Field, ForeignKey, Model, clear_registry, reset_engine
from ferro._annotation_utils import annotation_allows_none
from ferro.migrations import get_metadata
from ferro.query import BackRef


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield


def test_annotation_allows_none_primitive_and_union():
    assert annotation_allows_none(int) is False
    assert annotation_allows_none(int | None) is True
    assert annotation_allows_none(Union[str, None]) is True
    assert annotation_allows_none(Annotated[int | None, "x"]) is True
    assert annotation_allows_none(Annotated[int, "x"]) is False


def test_annotation_allows_none_type_alias():
    maybe_int = TypeAliasType("MaybeInt", int | None)

    assert annotation_allows_none(maybe_int) is True


def test_infer_assignment_int_required_with_field_default():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: int = Field(default=0)

    assert Row.__ferro_schema__["properties"]["field_a"]["ferro_nullable"] is False
    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is False


def test_infer_assignment_int_optional_union():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: int | None = None

    assert Row.__ferro_schema__["properties"]["field_a"]["ferro_nullable"] is True
    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_infer_assignment_optional_union_with_non_none_default_stays_nullable():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: int | None = 0

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_infer_type_alias_optional_field_is_nullable():
    maybe_int = TypeAliasType("MaybeInt", int | None)

    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: maybe_int = None

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_infer_annotated_int_field_default():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int, Field(default=0)]

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is False


def test_infer_annotated_int_union_none_field():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int | None, Field(default=None)]

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_infer_annotated_int_ferrofield():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int, FerroField(unique=True)]

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is False


def test_infer_annotated_int_union_none_ferrofield():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int | None, FerroField()]

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_infer_datetime_default_factory_not_nullable():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    t = get_metadata().tables["row"]
    assert t.c.created_at.nullable is False


def test_infer_enum_default_not_nullable():
    class Status(StrEnum):
        DRAFT = "draft"
        ACTIVE = "active"

    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: Status = Status.DRAFT

    assert Row.__ferro_schema__["properties"]["status"]["ferro_nullable"] is False
    t = get_metadata().tables["row"]
    assert t.c.status.nullable is False


def test_infer_fk_shadow_required():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildReq"]] = None

    class ChildReq(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[Parent, ForeignKey(related_name="children")]

    assert ChildReq.__ferro_schema__["properties"]["parent_id"]["ferro_nullable"] is False
    t = get_metadata().tables["childreq"]
    assert t.c.parent_id.nullable is False


def test_required_fk_shadow_rejects_missing_value():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildReqValidation"]] = None

    class ChildReqValidation(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[Parent, ForeignKey(related_name="children")]

    with pytest.raises(ValidationError):
        ChildReqValidation(id=1)

    with pytest.raises(ValidationError):
        ChildReqValidation(id=1, parent=None)

    with pytest.raises(ValidationError):
        ChildReqValidation(id=1, parent_id=None)


def test_infer_fk_shadow_optional():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildOpt"]] = None

    class ChildOpt(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[Parent | None, ForeignKey(related_name="children")] = None

    assert ChildOpt.__ferro_schema__["properties"]["parent_id"]["ferro_nullable"] is True
    t = get_metadata().tables["childopt"]
    assert t.c.parent_id.nullable is True


def test_override_ferrofield_nullable_false_on_optional_type():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int | None, FerroField(nullable=False)] = None

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is False


def test_override_field_nullable_false_on_optional_type():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: int | None = Field(default=None, nullable=False)

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is False
    assert Row.__ferro_schema__["properties"]["field_a"]["ferro_nullable"] is False


def test_override_ferrofield_nullable_true_on_required_type():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: Annotated[int, FerroField(nullable=True)] = 0

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_override_field_nullable_true_on_required_type():
    class Row(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        field_a: int = Field(default=0, nullable=True)

    t = get_metadata().tables["row"]
    assert t.c.field_a.nullable is True


def test_override_foreign_key_nullable_false_optional_relation():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildOv"]] = None

    class ChildOv(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[
            Parent | None,
            ForeignKey(related_name="children", nullable=False),
        ] = None

    t = get_metadata().tables["childov"]
    assert t.c.parent_id.nullable is False


def test_override_foreign_key_nullable_true_required_relation():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildFkTrue"]] = None

    class ChildFkTrue(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[
            Parent,
            ForeignKey(related_name="children", nullable=True),
        ]

    t = get_metadata().tables["childfktrue"]
    assert t.c.parent_id.nullable is True


def test_on_delete_set_null_infers_nullable_shadow_fk():
    class Parent(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        children: BackRef[list["ChildSetNull"]] = None

    class ChildSetNull(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        parent: Annotated[
            Parent,
            ForeignKey(related_name="children", on_delete="SET NULL"),
        ]

    t = get_metadata().tables["childsetnull"]
    assert t.c.parent_id.nullable is True
