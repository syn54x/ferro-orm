"""Tests for ferro._shadow_fk_types."""

import warnings
from typing import Annotated, Union
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import BackRef, FerroField, Field, ForeignKey, Model, connect
from ferro._shadow_fk_types import (
    is_fallback_shadow_annotation,
    pk_python_type_for_model,
    shadow_annotation_for_foreign_key,
    shadow_annotation_for_pk,
)
from ferro.base import ForeignKey as ForeignKeyCls


def test_pk_python_type_for_model_uuid():
    class UuidPkParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str

    assert pk_python_type_for_model(UuidPkParent) is UUID


def test_shadow_annotation_for_pk_wraps_required_uuid():
    ann = shadow_annotation_for_pk(UUID)
    assert ann == UUID | None


def test_is_fallback_shadow_annotation():
    assert is_fallback_shadow_annotation(Union[int, str, None])


def test_shadow_annotation_for_foreign_key_concrete():
    class IntPkParent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    fk = ForeignKeyCls(related_name="children")
    fk.to = IntPkParent
    assert shadow_annotation_for_foreign_key(fk) == (int | None)


def test_shadow_annotation_for_foreign_key_unresolved_string():
    fk = ForeignKeyCls(related_name="children")
    fk.to = "Parent"
    ann = shadow_annotation_for_foreign_key(fk)
    assert is_fallback_shadow_annotation(ann)


@pytest.fixture
def _cleanup_registry():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    yield
    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()


def test_reconcile_upgrades_forward_ref_shadow(_cleanup_registry):
    """Models self-register on class creation; do not hand-fill _MODEL_REGISTRY_PY."""
    from ferro.relations import resolve_relationships

    class ReconcileChild(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        parent: Annotated["ReconcileParent", ForeignKey(related_name="children")]

    class ReconcileParent(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        children: BackRef[list[ReconcileChild]] = None

    assert is_fallback_shadow_annotation(ReconcileChild.__annotations__["parent_id"])

    resolve_relationships()

    assert ReconcileChild.__annotations__["parent_id"] == (int | None)


@pytest.mark.asyncio
async def test_uuid_fk_create_get_dump():
    """Regression for GitHub #16: UUID PK through shadow FK without validation/serialization issues."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidIssueParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: BackRef[list["UuidIssueChild"]] = None

    class UuidIssueChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[UuidIssueParent, ForeignKey(related_name="children")]

    await connect("sqlite::memory:", auto_migrate=True)

    parent = await UuidIssueParent.create(name="p")
    child = await UuidIssueChild.create(parent=parent)

    fetched = await UuidIssueChild.get(child.id)
    assert fetched.parent_id == parent.id

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fetched.model_dump_json()
    unexpected = [
        x
        for x in w
        if x.category.__name__ == "PydanticSerializationUnexpectedValue"
    ]
    assert not unexpected


def test_nullable_fk_annotation_does_not_crash():
    from ferro.relations import resolve_relationships

    ferro.clear_registry()

    class NullableFkParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        children: BackRef[list["NullableFkChild"]] = None

    class NullableFkChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[NullableFkParent | None, ForeignKey(related_name="children")] = (
            None
        )

    resolve_relationships()
    assert NullableFkChild.ferro_relations["parent"].to is NullableFkParent
