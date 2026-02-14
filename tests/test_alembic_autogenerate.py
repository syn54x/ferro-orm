from typing import Annotated

import pytest

from ferro import FerroField, Model, clear_registry, reset_engine
from ferro.migrations import get_metadata


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield


def test_nullability_detection():
    """Verify that nullability is correctly detected from Pydantic types."""

    class User(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        email: str  # Required, non-nullable
        bio: str | None = None  # Optional, nullable

    metadata = get_metadata()
    user_table = metadata.tables["user"]

    assert user_table.c.email.nullable is False
    assert user_table.c.bio.nullable is True
    assert user_table.c.id.nullable is False


def test_schema_diff_simulation():
    """
    Simulate a schema change and verify that the metadata reflects the change.
    In a real app, Alembic would compare this metadata against the DB.
    """

    # 1. Initial State
    class Product(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        name: str

    meta_v1 = get_metadata()
    assert "price" not in meta_v1.tables["product"].c

    # 2. Simulate code change (adding a field)
    # We clear the registry and redefine to simulate a fresh run after a code edit
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    clear_registry()

    class Product(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        name: str
        price: float = 0.0

    meta_v2 = get_metadata()
    assert "price" in meta_v2.tables["product"].c
    assert meta_v2.tables["product"].c.price.nullable is False


def test_index_and_unique_diff():
    """Verify that adding an index/unique flag updates the metadata."""

    class Settings(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        key: str

    meta_v1 = get_metadata()
    assert meta_v1.tables["settings"].c.key.unique is False
    assert meta_v1.tables["settings"].c.key.index is False

    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    clear_registry()

    class Settings(Model):  # noqa
        id: Annotated[int, FerroField(primary_key=True)]
        key: Annotated[str, FerroField(unique=True, index=True)]

    meta_v2 = get_metadata()
    assert meta_v2.tables["settings"].c.key.unique is True
    assert meta_v2.tables["settings"].c.key.index is True
