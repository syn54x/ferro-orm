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


def test_registry_keys_by_qualified_identity():
    class QualifiedKeyModel(Model):
        __ferro_table__: ClassVar[str] = "qualified_key_model_a"
        id: int | None = None

    from ferro.state import _MODEL_REGISTRY_PY

    assert _MODEL_REGISTRY_PY[QualifiedKeyModel.__ferro_identity__] is QualifiedKeyModel
    assert "QualifiedKeyModel" not in _MODEL_REGISTRY_PY


def test_same_named_models_in_distinct_scopes_coexist():
    def make_a():
        class ScopedModel(Model):
            __ferro_table__: ClassVar[str] = "scoped_model_a"
            id: int | None = None

        return ScopedModel

    def make_b():
        class ScopedModel(Model):
            __ferro_table__: ClassVar[str] = "scoped_model_b"
            id: int | None = None

        return ScopedModel

    a, b = make_a(), make_b()
    from ferro.state import _MODEL_REGISTRY_PY

    assert _MODEL_REGISTRY_PY[a.__ferro_identity__] is a
    assert _MODEL_REGISTRY_PY[b.__ferro_identity__] is b


def test_resolve_model_reference_ambiguous_lists_candidates():
    def make_a():
        class AmbiguousRef(Model):
            __ferro_table__: ClassVar[str] = "ambiguous_ref_a"
            id: int | None = None

        return AmbiguousRef

    def make_b():
        class AmbiguousRef(Model):
            __ferro_table__: ClassVar[str] = "ambiguous_ref_b"
            id: int | None = None

        return AmbiguousRef

    a, b = make_a(), make_b()
    with pytest.raises(RuntimeError) as excinfo:
        resolve_model_reference("AmbiguousRef")
    message = str(excinfo.value)
    assert a.__ferro_identity__ in message
    assert b.__ferro_identity__ in message


def test_fk_string_ref_to_ambiguous_short_name_errors_with_candidates():
    from typing import Annotated

    from ferro import BackRef, Relation
    from ferro.base import ForeignKey
    from ferro.relations import resolve_relationships

    # Two distinct enclosing scopes give the same-named targets distinct
    # __qualname__s (hence distinct identities) while sharing the bare name
    # "FkTarget" — exactly the collision that makes the short FK ref ambiguous.
    def make_a():
        class FkTarget(Model):
            __ferro_table__: ClassVar[str] = "fk_target_one"
            id: int | None = None
            refs: Relation[list["FkSource"]] = BackRef()

        return FkTarget

    def make_b():
        class FkTarget(Model):
            __ferro_table__: ClassVar[str] = "fk_target_two"
            id: int | None = None
            refs: Relation[list["FkSource"]] = BackRef()

        return FkTarget

    t1 = make_a()
    t2 = make_b()

    class FkSource(Model):
        id: int | None = None
        target: Annotated["FkTarget", ForeignKey(related_name="refs")]  # noqa: F821

    with pytest.raises(RuntimeError) as excinfo:
        resolve_relationships()
    message = str(excinfo.value)
    assert t1.__ferro_identity__ in message
    assert t2.__ferro_identity__ in message
