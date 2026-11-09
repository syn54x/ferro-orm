# A field's database options live in its metadata

```python
class Player(Model):
    id: int | None = Field(default=None, primary_key=True)
    handle: str = Field(index=True, min_length=2)

Player.model_json_schema()["properties"]["handle"]
# {"minLength": 2, "title": "Handle", "type": "string"}
Player.__ferro_fields__["handle"].index
# True
```

A field's database options (`primary_key`, `unique`, `index`, `autoincrement`, `nullable`, `db_type`, `db_check`, `renamed_from`, `retired`, `generated`) are stored on the field, as one `FerroField` in its `FieldInfo.metadata`, and never in its JSON schema. `Field()` builds that `FerroField` and appends it; `Annotated[T, FerroField(...)]` puts the same object in the same place. The `generated` lambda rides inside it as `FerroField.generated`, so the separate carrier that held it goes. The metaclass collects each field's `FerroField` into `Model.__ferro_fields__`, and the column compiler reads the options from there alone. It still reads the JSON schema for types, formats, defaults and `enum_type_name`, which are Pydantic's facts about the field.

Until now `Field()` wrote its options into `json_schema_extra` under a `ferro_field` key, and Pydantic printed them into every schema. Litestar refuses to build an OpenAPI document from a model whose schema carries that key, a FastAPI client sees database settings it can do nothing with, and an option whose value is a class (the `foreign_key=Team` Epic #874 adds) would make `model_json_schema()` raise, since a class has no JSON form. The metadata list is where Pydantic keeps everything else about a field that is not JSON, and the place `generated=` already used for the same reason.

The raw path, a plain `json_schema_extra={"primary_key": True}` read as an option, is retired with it. A field whose `json_schema_extra` carries `primary_key`, `unique`, `index`, `autoincrement`, `db_type`, `db_check` or `ferro_nullable`, or the `ferro_field` dict the old `Field()` wrote, is refused when the class is defined, naming the `Field()` arguments: `Pass primary_key=True to Field() instead of json_schema_extra`. Any other key, such as `enum_type_name` or `examples`, passes through to the schema as before. `Field()` also refuses a keyword it does not take, naming the closest option (`Did you mean 'index'?`), instead of handing it to Pydantic's deprecated extra keywords, where a typo became a silent schema key. The known names come from `Field`'s own signature, so a new option is known the moment it is a parameter. The class stores take the dunder names of Ferro's other class attributes: `ferro_fields` becomes `__ferro_fields__` and `ferro_relations` becomes `__ferro_relations__`, with no alias.

The relation keywords (`back_ref`, `many_to_many`, `related_name`, `through`, `reverse_index`) leave the schema the same way, on a private carrier in the metadata, until Epic #874 removes them.

Decided in Epic #874 (2026-11-07), built in #875.

Rejected alternatives:

- **A callable `json_schema_extra` that strips the `ferro_field` key before Pydantic prints it**: the schema would come out clean, but the options would have two storage shapes, a dict for a plain `json_schema_extra` and a closure for a callable one, and a caller's own callable would have to be wrapped around Ferro's. It is also why `Field(index=True, json_schema_extra=<callable>)` used to raise; with the options in the metadata, the two combine.
- **Keep the options in the schema and document the key**: every OpenAPI consumer would need to filter it, and a class-valued option could never be added.
- **Keep the raw path beside the metadata**: two declarations of one fact, read by two rules, is what ADR-0002 and ADR-0003 spent their effort reconciling. With the options off the schema, a raw key would be printed into the schema and read by nothing, so it is refused rather than ignored.

Consequences:

- `model_json_schema()` carries nothing of Ferro's, for every option and every relation.
- ADR-0002 and ADR-0003 are superseded in part: there is one declaration path, read from the field's metadata, so the raw-path half of each no longer exists. Their unified autoincrement rule and path-blind `db_type` validation still hold for `Field()` and `FerroField()`.
- A model that declared options through `json_schema_extra` fails at import, naming each `Field()` argument to use; the schema it compiles to is unchanged once converted.
