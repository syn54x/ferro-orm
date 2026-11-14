from ferro import Field, Model


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
