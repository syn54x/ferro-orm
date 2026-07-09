"""FF-E E4: resolve_relationships' schema re-registration pass fails loudly.

The selective re-registration pass used to wrap every re-registration in
``except Exception: pass``, leaving a model that failed to rebuild silently
on its pre-relationship schema.
"""

from typing import Annotated

import pytest

from ferro import BackRef, ForeignKey, Model, Relation
from ferro.relations import resolve_relationships


@pytest.fixture(autouse=True)
def _isolate_relation_state():
    from ferro.state import _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    models_snapshot = dict(_MODEL_REGISTRY_PY)
    relations_snapshot = list(_PENDING_RELATIONS)
    yield
    _MODEL_REGISTRY_PY.clear()
    _MODEL_REGISTRY_PY.update(models_snapshot)
    _PENDING_RELATIONS.clear()
    _PENDING_RELATIONS.extend(relations_snapshot)


def test_second_pass_rebuild_failure_aborts_with_model_named(monkeypatch):
    class E4Broken(Model):
        id: int | None = None
        name: str
        peers: Relation[list["E4Peer"]] = BackRef()

    class E4Peer(Model):
        id: int | None = None
        broken_id: int | None = None
        broken: Annotated[E4Broken, ForeignKey(related_name="peers")]

    import ferro.relations as relations_mod

    real_compile = relations_mod.compile_model_schema_ir

    def failing_compile(model_name, model_cls, specs=None):
        if model_cls.__name__ == "E4Broken":
            raise ValueError("boom: schema rebuild failed")
        return real_compile(model_name, model_cls, specs)

    monkeypatch.setattr(relations_mod, "compile_model_schema_ir", failing_compile)

    with pytest.raises(RuntimeError) as excinfo:
        resolve_relationships()

    assert "E4Broken" in str(excinfo.value)
    assert "boom: schema rebuild failed" in str(excinfo.value)
