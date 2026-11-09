---
title: A nested model serializes without calling its model_dump()
type: issue
tags: [python, pydantic, relationships, gotcha]
related_files:
  - src/ferro/_unloaded.py
  - src/ferro/models.py
related_issues: [878, 917]
captured: 2026-11-09
---

# A nested model serializes without calling its model_dump()

An override of `model_dump()` guards only the instance it is called on.
Guard serialization with a wrap `model_serializer` instead.

```python
class PlayerOut(BaseModel):
    team: Team

PlayerOut(team=player.team).model_dump()   # never calls Team.model_dump()
TypeAdapter(list[Team]).dump_json([team])  # nor does this, nor a FastAPI response model
```

pydantic-core serializes a nested model, a `TypeAdapter` value and a response
model straight from its `__dict__`, and it skips a field missing from that dict
without raising. So an unloaded instance (ADR-0063), whose `__dict__` holds only
its key, came out as `{"id": 1}` everywhere but its own `model_dump()`.

`Model` carries `@model_serializer(mode="wrap")` (`_refuse_unloaded`), which
every serialization path runs. Two Pydantic facts shape it:

- pydantic-core wraps whatever a serializer function raises in a
  `PydanticSerializationError` ("Error calling function `_refuse_unloaded`:
  InstanceNotLoaded: …"), so only the overrides of `model_dump()` and
  `model_dump_json()` raise `InstanceNotLoaded` itself.
- The serializer has no return annotation. One would become the model's
  serialization JSON schema; without one, Pydantic keeps the fields' schema.

## How to recognize

A refusal or redaction that lives in a `model_dump()` override, tested only by
calling `model_dump()` on the instance. Test it nested in a `BaseModel` and
through `TypeAdapter(...).dump_json` too.
