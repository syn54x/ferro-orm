"""FF-D D5: a relation keyword's refusal names the *target* model's PK field.

The constructor takes the foreign-key column only; refusing `warehouse=` names
`warehouse_id=warehouse.code`, the target's own key, not the source's `id`.
"""

import pytest


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
