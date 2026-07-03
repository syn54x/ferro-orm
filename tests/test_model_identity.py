"""FF-E E1/E2: model identity stamps and reference resolution."""

from typing import ClassVar

import pytest

from ferro import Model
from ferro.state import resolve_model_reference


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


def test_models_are_stamped_with_identity_and_table():
    class IdentityUser(Model):
        id: int | None = None

    assert IdentityUser.__ferro_identity__ == (
        f"{IdentityUser.__module__}.{IdentityUser.__qualname__}"
    )
    assert IdentityUser.__ferro_table__ == "identityuser"


def test_ferro_table_overrides_default():
    class TableUser(Model):
        __ferro_table__: ClassVar[str] = "app_users"
        id: int | None = None

    assert TableUser.__ferro_table__ == "app_users"


def test_ferro_table_is_not_inherited():
    class TableParent(Model):
        __ferro_table__: ClassVar[str] = "custom_parent"
        id: int | None = None

    class TableChild(TableParent):
        pass

    assert TableChild.__ferro_table__ == "tablechild"


def test_ferro_table_rejects_non_string():
    with pytest.raises(TypeError, match="__ferro_table__"):

        class BadType(Model):
            __ferro_table__: ClassVar[int] = 7
            id: int | None = None


@pytest.mark.parametrize("bad", ["", "1starts_with_digit", "has space", "x" * 64])
def test_ferro_table_rejects_invalid_names(bad):
    with pytest.raises(ValueError, match="__ferro_table__"):
        type("BadTable", (Model,), {"__ferro_table__": bad, "__annotations__": {"id": int | None}, "id": None})


def test_resolve_model_reference_short_and_qualified():
    class RefTarget(Model):
        id: int | None = None

    assert resolve_model_reference("RefTarget") is RefTarget
    assert resolve_model_reference(RefTarget.__ferro_identity__) is RefTarget


def test_resolve_model_reference_not_found():
    with pytest.raises(RuntimeError, match="NoSuchModelAnywhere"):
        resolve_model_reference("NoSuchModelAnywhere")
    assert resolve_model_reference("NoSuchModelAnywhere", default=None) is None
