"""Tests for ferro._shadow_fk_types."""

import warnings
from typing import Annotated, Union
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import BackRef, FerroField, Field, ForeignKey, Model, Relation, connect
from ferro._shadow_fk_types import (
    is_fallback_shadow_annotation,
    pk_python_type_for_model,
    shadow_annotation_for_foreign_key,
    shadow_annotation_for_pk,
)
from ferro.base import ForeignKey as ForeignKeyCls

pytestmark = pytest.mark.backend_matrix


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
        children: Relation[list[ReconcileChild]] = BackRef()

    assert is_fallback_shadow_annotation(ReconcileChild.__annotations__["parent_id"])

    resolve_relationships()

    assert ReconcileChild.__annotations__["parent_id"] == (int | None)
    assert ReconcileChild.model_fields["parent_id"].annotation == (int | None)


@pytest.mark.asyncio
async def test_uuid_fk_create_get_dump(db_url):
    """Regression for GitHub #16: UUID PK through shadow FK without validation/serialization issues."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidIssueParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list["UuidIssueChild"]] = BackRef()

    class UuidIssueChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[UuidIssueParent, ForeignKey(related_name="children")]

    await connect(db_url, auto_migrate=True)

    parent = await UuidIssueParent.create(name="p")
    child = await UuidIssueChild.create(parent=parent)

    fetched = await UuidIssueChild.get(child.id)
    assert fetched.parent_id == parent.id

    by_shadow = await UuidIssueChild.where(
        UuidIssueChild.parent_id == parent.id
    ).first()
    assert by_shadow is not None
    assert by_shadow.id == child.id

    for dumper in (fetched.model_dump, fetched.model_dump_json):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            dumper()
        unexpected = [
            x
            for x in w
            if x.category.__name__ == "PydanticSerializationUnexpectedValue"
        ]
        assert not unexpected


@pytest.mark.asyncio
async def test_uuid_fk_forward_ref_child_declared_first(db_url):
    """Circular-import style: child model references parent by string before parent exists."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidFrwChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated["UuidFrwParent", ForeignKey(related_name="children")]

    class UuidFrwParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list[UuidFrwChild]] = BackRef()

    await connect(db_url, auto_migrate=True)

    parent = await UuidFrwParent.create(name="p")
    child = await UuidFrwChild.create(parent=parent)
    fetched = await UuidFrwChild.get(child.id)
    assert fetched.parent_id == parent.id


def test_uuid_child_model_validate_accepts_string_parent_id():
    """API / JSON payloads often send UUID FKs as strings; shadow typing should coerce."""
    ferro.clear_registry()

    class VParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list["VChild"]] = BackRef()

    class VChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[VParent, ForeignKey(related_name="children")]

    from ferro.relations import resolve_relationships

    resolve_relationships()

    pid = uuid4()
    cid = uuid4()
    row = VChild.model_validate({"id": cid, "parent_id": str(pid)})
    assert row.parent_id == pid
    assert isinstance(row.parent_id, UUID)


def test_nullable_fk_annotation_does_not_crash():
    from ferro.relations import resolve_relationships

    ferro.clear_registry()

    class NullableFkParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        children: Relation[list["NullableFkChild"]] = BackRef()

    class NullableFkChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent: Annotated[
            NullableFkParent | None, ForeignKey(related_name="children")
        ] = None

    resolve_relationships()
    assert NullableFkChild.ferro_relations["parent"].to is NullableFkParent


@pytest.mark.asyncio
async def test_uuid_fk_save_after_reparenting(db_url):
    """Update an existing row: change only the UUID foreign key, then save() and re-fetch."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidMutParent(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        kids: Relation[list["UuidMutChild"]] = BackRef()

    class UuidMutChild(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        label: str
        parent: Annotated[UuidMutParent, ForeignKey(related_name="kids")]

    await connect(db_url, auto_migrate=True)

    parent_a = await UuidMutParent.create(name="a")
    parent_b = await UuidMutParent.create(name="b")
    child = await UuidMutChild.create(label="row", parent=parent_a)
    assert child.parent_id == parent_a.id

    child.parent_id = parent_b.id
    await child.save()

    refetched = await UuidMutChild.get(child.id)
    assert refetched is not None
    assert refetched.parent_id == parent_b.id
    assert refetched.label == "row"


@pytest.mark.asyncio
async def test_uuid_fk_bulk_create(db_url):
    """bulk_create serializes UUID FK columns via model_dump(mode='json')."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidBulkParent(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        items: Relation[list["UuidBulkItem"]] = BackRef()

    class UuidBulkItem(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        sku: str
        parent: Annotated[UuidBulkParent, ForeignKey(related_name="items")]

    await connect(db_url, auto_migrate=True)

    px = await UuidBulkParent.create(name="x")
    py = await UuidBulkParent.create(name="y")

    rows = [
        UuidBulkItem(sku="1", parent_id=px.id),
        UuidBulkItem(sku="2", parent_id=py.id),
        UuidBulkItem(sku="3", parent_id=px.id),
    ]
    inserted = await UuidBulkItem.bulk_create(rows)
    assert inserted == 3

    all_items = await UuidBulkItem.all()
    assert len(all_items) == 3
    by_sku = {item.sku: item for item in all_items}
    # Hydration may return UUID or TEXT from SQLite; compare normalized strings.
    assert str(by_sku["1"].parent_id) == str(px.id)
    assert str(by_sku["2"].parent_id) == str(py.id)
    assert str(by_sku["3"].parent_id) == str(px.id)
