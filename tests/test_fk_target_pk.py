"""FF-D D5: relationship inputs must read the *target* model's PK field.

Today `Model.__init__` scans the source class's ferro_fields for the PK name
and reads that name off the target instance — correct only while every PK is
named `id`.
"""

from typing import Annotated

import pytest

from ferro import Field, Model
from ferro.base import ForeignKey


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    """Restore the global relation registries after each test.

    Each test below declares its models *inside the test function*, but
    `ModelMetaclass` still appends their `ForeignKey` metadata to the
    module-level `_PENDING_RELATIONS` list and registers the classes in
    `_MODEL_REGISTRY_PY` (see `src/ferro/metaclass.py`) unconditionally at
    class-creation time -- nothing about being defined inside a function
    scopes that registration. Nothing ever removes those entries once the
    test function returns, so without this fixture the test-local
    `D5Warehouse`/`D5Shipment`/`D5Author`/`D5Book` FK metadata leaks into
    later tests: the next `connect(auto_migrate=True)` anywhere in the
    suite calls `resolve_relationships()`, which replays *every* pending
    relation, including these stale ones, and blows up because their
    target models never declared the matching `BackRef()`.
    """
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS, deregister_model

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    relations_snapshot = list(_PENDING_RELATIONS)
    yield
    # Evict test-local models through the entrypoint so their envelope caches go
    # with them, then restore the baseline snapshot (bulk teardown restore).
    for name in set(_MODEL_REGISTRY_PY) - set(models_snapshot):
        deregister_model(name)
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(relations_snapshot)


def test_fk_extraction_uses_target_pk_name():
    class D5Warehouse(Model):
        code: int | None = Field(default=None, primary_key=True)
        city: str

    class D5Shipment(Model):
        id: int | None = Field(default=None, primary_key=True)
        warehouse: Annotated[D5Warehouse, ForeignKey(related_name="shipments")]

    wh = D5Warehouse(code=7, city="Reno")
    shipment = D5Shipment(warehouse=wh)
    assert shipment.warehouse_id == 7


def test_fk_extraction_with_target_pk_named_id_still_works():
    class D5Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class D5Book(Model):
        id: int | None = Field(default=None, primary_key=True)
        author: Annotated[D5Author, ForeignKey(related_name="books")]

    a = D5Author(id=3, name="x")
    b = D5Book(author=a)
    assert b.author_id == 3
