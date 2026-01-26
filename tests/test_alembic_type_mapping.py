from decimal import Decimal
from enum import Enum
from typing import Annotated, Dict, List
from uuid import UUID

import pytest
import sqlalchemy as sa

from ferro import FerroField, Model, clear_registry, reset_engine
from ferro.migrations import get_metadata


class UserStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@pytest.fixture(autouse=True)
def cleanup():
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield


def test_complex_type_mapping():
    """Verify that complex types like Enum, Decimal, JSON, and UUID are mapped correctly."""

    class ComplexModel(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: UserStatus
        price: Decimal
        token: UUID
        metadata: Dict[str, str]
        tags: List[str]

    metadata = get_metadata()
    table = metadata.tables["complexmodel"]

    # Enum
    assert isinstance(table.c.status.type, sa.Enum)
    assert set(table.c.status.type.enums) == {"active", "inactive"}

    # Numeric/Decimal
    assert isinstance(table.c.price.type, (sa.Numeric, sa.Float))

    # UUID
    assert isinstance(table.c.token.type, (sa.Uuid, sa.String))

    # JSON (Dict and List)
    assert isinstance(table.c.metadata.type, sa.JSON)
    assert isinstance(table.c.tags.type, sa.JSON)


def test_optional_complex_types():
    """Verify that Optional complex types are still mapped correctly."""

    class OptionalComplexModel(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        status: UserStatus | None = None
        price: Decimal | None = None

    metadata = get_metadata()
    table = metadata.tables["optionalcomplexmodel"]

    assert isinstance(table.c.status.type, sa.Enum)
    assert table.c.status.nullable is True
    assert isinstance(table.c.price.type, (sa.Numeric, sa.Float))
    assert table.c.price.nullable is True
