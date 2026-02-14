import os
import uuid
from typing import Annotated

import pytest

from ferro import FerroField, Field, Model, connect


@pytest.fixture
def db_url():
    db_file = f"test_field_wrapper_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


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


def test_annotated_and_wrapped_ferro_field_conflict_raises():
    with pytest.raises(TypeError, match="cannot declare Ferro field metadata twice"):

        class InvalidUser(Model):
            id: Annotated[int, FerroField(primary_key=True)] = Field(primary_key=True)
