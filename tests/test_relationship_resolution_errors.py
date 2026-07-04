"""FF-E E4: resolve_relationships' schema re-registration pass fails loudly.

The second pass used to wrap every re-registration in `except Exception:
pass`, leaving a model that failed to rebuild silently on its
pre-relationship schema.
"""

import pytest

from ferro import Model
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

    import ferro.relations as relations_mod

    real_build = relations_mod.build_model_schema

    def failing_build(model_cls, schema=None):
        if model_cls.__name__ == "E4Broken":
            raise ValueError("boom: schema rebuild failed")
        return real_build(model_cls)

    monkeypatch.setattr(relations_mod, "build_model_schema", failing_build)

    with pytest.raises(RuntimeError) as excinfo:
        resolve_relationships()

    assert "E4Broken" in str(excinfo.value)
    assert "boom: schema rebuild failed" in str(excinfo.value)
