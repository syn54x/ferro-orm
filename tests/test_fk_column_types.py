"""Tests for ferro._fk_column_types."""

import warnings
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import ferro
from ferro import (
    FerroField,
    Field,
    Forward,
    ManyToMany,
    Model,
    Relation,
    Reverse,
    connect,
)
from ferro._fk_column_types import pk_python_type_for_model

pytestmark = pytest.mark.backend_matrix


def test_pk_python_type_for_model_uuid():
    class UuidPkParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str

    assert pk_python_type_for_model(UuidPkParent) is UUID


def test_a_join_table_types_its_columns_as_each_model_s_primary_key():
    from ferro.registry import REGISTRY
    from ferro.relations import resolve_relationships

    ferro.clear_registry()

    class JoinUuidTag(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        posts: Relation[list["JoinIntPost"]] = Reverse("tags")

    class JoinIntPost(Model):
        id: int | None = Field(default=None, primary_key=True)
        tags: Relation[list[JoinUuidTag]] = ManyToMany(through="join_post_tags")

    resolve_relationships()

    columns = {
        spec.name: (spec.logical_type, spec.python_type, spec.foreign_key.to_table)
        for spec in REGISTRY.join_tables()["join_post_tags"]["columns"]
    }
    assert columns == {
        "joinintpost_id": ("integer", int, "joinintpost"),
        "joinuuidtag_id": ("uuid", UUID, "joinuuidtag"),
    }


@pytest.mark.asyncio
async def test_uuid_fk_create_get_dump(db_url):
    """Regression for GitHub #16: UUID PK through a foreign-key column without validation/serialization issues."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidIssueParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list["UuidIssueChild"]] = Reverse("parent_id")

    class UuidIssueChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent_id: UUID = Field(foreign_key=UuidIssueParent)
        parent: Relation[UuidIssueParent] = Forward("parent_id")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        parent = await UuidIssueParent.create(name="p")
        # An API payload carries the UUID key as a string.
        child = await UuidIssueChild.create(parent_id=str(parent.id))

        fetched = await UuidIssueChild.get(child.id)
        assert fetched.parent_id == parent.id

        by_fk_column = await UuidIssueChild.where(
            lambda uuid_issue_child: uuid_issue_child.parent_id == parent.id
        ).first()
        assert by_fk_column is not None
        assert by_fk_column.id == child.id

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
        parent_id: UUID = Field(foreign_key="UuidFrwParent")
        parent: Relation["UuidFrwParent"] = Forward("parent_id")

    class UuidFrwParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list[UuidFrwChild]] = Reverse("parent_id")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        parent = await UuidFrwParent.create(name="p")
        child = await UuidFrwChild.create(parent_id=parent.id)
        fetched = await UuidFrwChild.get(child.id)
        assert fetched.parent_id == parent.id


def test_uuid_child_model_validate_accepts_string_parent_id():
    """API / JSON payloads send UUID keys as strings; the foreign-key column coerces."""
    ferro.clear_registry()

    class VParent(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        name: str
        children: Relation[list["VChild"]] = Reverse("parent_id")

    class VChild(Model):
        id: UUID = Field(default_factory=uuid4, primary_key=True)
        parent_id: UUID = Field(foreign_key=VParent)
        parent: Relation[VParent] = Forward("parent_id")

    from ferro.relations import resolve_relationships

    resolve_relationships()

    pid = uuid4()
    cid = uuid4()
    row = VChild.model_validate({"id": cid, "parent_id": str(pid)})
    assert row.parent_id == pid
    assert isinstance(row.parent_id, UUID)


@pytest.mark.asyncio
async def test_uuid_fk_save_after_reparenting(db_url):
    """Update an existing row: change only the UUID foreign key, then save() and re-fetch."""
    ferro.reset_engine()
    ferro.clear_registry()

    class UuidMutParent(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        name: str
        kids: Relation[list["UuidMutChild"]] = Reverse("parent_id")

    class UuidMutChild(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        label: str
        parent_id: UUID = Field(foreign_key=UuidMutParent)
        parent: Relation[UuidMutParent] = Forward("parent_id")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        parent_a = await UuidMutParent.create(name="a")
        parent_b = await UuidMutParent.create(name="b")
        child = await UuidMutChild.create(label="row", parent_id=parent_a.id)
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
        items: Relation[list["UuidBulkItem"]] = Reverse("parent_id")

    class UuidBulkItem(Model):
        id: Annotated[UUID, FerroField(primary_key=True)] = Field(default_factory=uuid4)
        sku: str
        parent_id: UUID = Field(foreign_key=UuidBulkParent)
        parent: Relation[UuidBulkParent] = Forward("parent_id")

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
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
