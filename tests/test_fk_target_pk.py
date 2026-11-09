"""FF-D D5: a relation keyword's refusal names the *target* model's PK field.

The constructor takes the foreign-key column only; refusing `warehouse=` names
`warehouse_id=warehouse.code`, the target's own key, not the source's `id`.
"""

import pytest

from ferro import Field, Forward, Model, Relation


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    """Restore the global relation registries after each test.

    Each test below declares its models *inside the test function*, but
    `ModelMetaclass` still queues their relation metadata on the
    Registry's pending-relations queue and registers the classes in
    the model registry (see `src/ferro/metaclass.py`) unconditionally at
    class-creation time -- nothing about being defined inside a function
    scopes that registration. Nothing ever removes those entries once the
    test function returns, so without this fixture the test-local
    `D5Warehouse`/`D5Shipment`/`D5Author`/`D5Book` FK metadata leaks into
    later tests: the next `connect(auto_migrate=True)` anywhere in the
    suite calls `resolve_relationships()`, which replays *every* pending
    relation, including these stale ones.
    """
    from ferro.registry import REGISTRY

    snapshot = REGISTRY.snapshot()
    yield
    REGISTRY.restore(snapshot)


def test_relation_keyword_refusal_names_the_target_pk():
    class D5Warehouse(Model):
        code: int | None = Field(default=None, primary_key=True)
        city: str

    class D5Shipment(Model):
        id: int | None = Field(default=None, primary_key=True)
        warehouse_id: int = Field(foreign_key=D5Warehouse)
        warehouse: Relation[D5Warehouse] = Forward("warehouse_id")

    wh = D5Warehouse(code=7, city="Reno")
    with pytest.raises(TypeError) as refused:
        D5Shipment(warehouse=wh)
    assert str(refused.value) == (
        "D5Shipment.warehouse is a relation. Pass the foreign-key column's "
        "value, warehouse_id=warehouse.code"
    )
    # The fix the refusal names stores the target's key.
    assert D5Shipment(warehouse_id=wh.code).warehouse_id == 7


def test_relation_keyword_refusal_with_target_pk_named_id():
    class D5Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class D5Book(Model):
        id: int | None = Field(default=None, primary_key=True)
        author_id: int = Field(foreign_key=D5Author)
        author: Relation[D5Author] = Forward("author_id")

    a = D5Author(id=3, name="x")
    with pytest.raises(TypeError, match=r"author_id=author\.id$"):
        D5Book(author=a)
    assert D5Book(author_id=a.id).author_id == 3
