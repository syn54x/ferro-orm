from typing import Annotated

import pytest

from ferro import FerroField, Field, Model, connect

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_ferro_field_wrapper_sets_metadata_and_pydantic_schema(db_url):
    class WrappedUser(Model):
        id: int | None = Field(
            default=None, primary_key=True, description="Primary key"
        )
        email: str = Field(unique=True, index=True, description="Email address")

    # Ferro metadata is captured for both fields.
    assert WrappedUser.ferro_fields["id"].primary_key is True
    assert WrappedUser.ferro_fields["email"].unique is True
    assert WrappedUser.ferro_fields["email"].index is True

    # Pydantic metadata still works via wrapped Field.
    schema = WrappedUser.model_json_schema()
    assert schema["properties"]["id"]["description"] == "Primary key"
    assert schema["properties"]["email"]["description"] == "Email address"

    await connect(db_url, auto_migrate=True)
    user = WrappedUser(email="one@example.com")
    await user.save()
    assert user.id is not None


def test_assignment_and_annotation_field_patterns_equivalent_ferro_metadata():
    """Assignment `x: T = Field(...)` and annotation `x: Annotated[T, Field(...)]` must
    yield the same Ferro column metadata (Pydantic merges Annotated Field into FieldInfo).
    """

    class ByAssignment(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: int = Field(default=0, unique=True, index=True)
        name: str

    class ByAnnotation(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        code: Annotated[int, Field(default=0, unique=True, index=True)]
        name: str

    assert set(ByAssignment.ferro_fields) == set(ByAnnotation.ferro_fields)
    for fname in ByAssignment.ferro_fields:
        a = ByAssignment.ferro_fields[fname]
        b = ByAnnotation.ferro_fields[fname]
        assert (a.primary_key, a.unique, a.index, a.autoincrement) == (
            b.primary_key,
            b.unique,
            b.index,
            b.autoincrement,
        )


@pytest.mark.asyncio
async def test_annotation_field_pattern_persists_like_assignment(db_url):
    """Annotated[..., Field(...)] should behave the same as assignment for DB round-trip."""

    class AnnotatedUser(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        email: Annotated[
            str, Field(unique=True, index=True, description="Login email")
        ]

    assert AnnotatedUser.ferro_fields["id"].primary_key is True
    assert AnnotatedUser.ferro_fields["email"].unique is True
    schema = AnnotatedUser.model_json_schema()
    assert schema["properties"]["email"]["description"] == "Login email"

    await connect(db_url, auto_migrate=True)
    user = AnnotatedUser(email="ann@example.com")
    await user.save()
    assert user.id is not None


@pytest.mark.asyncio
async def test_optional_patterned_string_roundtrip(db_url):
    class WrappedInventory(Model):
        id: int | None = Field(default=None, primary_key=True)
        code: str | None = Field(default=None, pattern=r"^SKU-[0-9]+$")

    await connect(db_url, auto_migrate=True)
    item = await WrappedInventory.create(code="SKU-123")
    fetched = await WrappedInventory.get(item.id)

    assert fetched is not None
    assert fetched.code == "SKU-123"


def test_annotated_and_wrapped_ferro_field_conflict_raises():
    with pytest.raises(TypeError, match="cannot declare Ferro field metadata twice"):

        class InvalidUser(Model):
            id: Annotated[int, FerroField(primary_key=True)] = Field(primary_key=True)
