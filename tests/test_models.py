import pytest
from ferro import Model, connect
from pydantic import Field
import json

def test_model_registration():
    """Test that defining a model registers it with the Rust engine."""
    class TestUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str
        email: str | None = None

    # If we got here without error, the metaclass successfully called Rust
    assert TestUser.__name__ == "TestUser"
    assert "id" in TestUser.model_fields
    assert "username" in TestUser.model_fields

def test_duplicate_model_registration():
    """Test that re-defining a model doesn't crash."""
    class DuplicateModel(Model):
        name: str
    
    class DuplicateModel(Model):
        name: str
        age: int
    
    assert "age" in DuplicateModel.model_fields

def test_model_json_schema_sent_to_rust():
    """
    Test that the schema is valid and contains our fields.
    (Indirectly testing the metaclass logic)
    """
    class SchemaModel(Model):
        tag: str = Field(max_length=10)
    
    schema = SchemaModel.model_json_schema()
    assert "tag" in schema["properties"]
    assert schema["properties"]["tag"]["maxLength"] == 10
