# An instance is complete or unloaded

```python
player = await Player.get(1)
player.team                        # Team(id=1, unloaded): a real Team, no query
player.team.id                     # 1, read from player.team_id
player.team.name                   # InstanceNotLoaded: Team(id=1) is not loaded. Read it after
                                   # include(lambda player: player.team) or await player.team.load()
team = await player.team.load()    # one SELECT, fills the same object and returns it
team is await Team.get(1)          # True inside a session: one row, one object
```

A model instance is in one of two states. A **complete** instance carries its whole row, as every instance did under ADR-0007. An **unloaded** instance carries only its primary key, and exists only behind a forward relation nothing loaded. Nothing sits between the two: an unloaded instance refuses every other column read, every column assignment, `save()`, `model_dump()` and `model_dump_json()` with `InstanceNotLoaded`, one error that is both a `FerroError` and an `AttributeError` (so `hasattr()` and `getattr(..., default)` keep their meaning), and whose message names the two ways to load it. It allows its key, `.load()`, `delete()`, Pydantic's `==` unchanged, and a `repr` of `Team(id=1, unloaded)`.

Until now a forward relation nothing included read as a coroutine: `team = await player.team`. Its type depended on the query that produced the row, `Team` after `include()` and an awaitable otherwise, so `player.team: Team` was a static lie half the time and typed code needed ignore comments (#290). Now `player.team` is always a `Team`.

The first read of a relation looks its key up in the session's identity map. A `Team(1)` the session holds comes back as that object, complete if it is. Otherwise the Rust hydrator builds the unloaded instance the way it builds every instance (`cls.__new__`, the key in `__dict__`, the Pydantic slots, the connection and persisted markers, since the key vouches for the row) plus one marker the refusals read, registers it under its key and caches it on the parent. A later `Team.get(1)`, query or include of that row fills the same object in place and clears the marker; `.load()` does the same on demand, and on a complete instance sends nothing. Without a session the instance is cached on the parent alone. A null key reads as `None`.

An include whose non-null key matches no row, because the row is gone or row security hides it, returns the parent row with the relation unloaded, holding the key, and its refusal says the query found no row. `.load()` on it, and on any unloaded instance whose row is gone, raises `ModelDoesNotExist`; `refresh()` on a gone row raises the same error instead of `RuntimeError`.

Decided in Epic #874 (2026-11-07, from #868), built in #878.

Rejected alternatives:

- **A stand-in object that is not a `Team`** (a proxy or reference holding the key): two objects for one row, the stand-in and the `Team` a fetch returns, so identity breaks exactly where the identity map promises it. The stand-in would have to imitate `==`, `hash`, `repr`, `isinstance` and `model_dump`, and every one it gets wrong is a bug at a distance. A real `Team` gets all of them from the class, and the identity map makes it the one object for its row.
- **Keep the awaitable** (`await player.team`): the type keeps depending on the query, and `await` on a model would have to mean "maybe I/O" everywhere (ADR-0008 rejected `Model.__await__` for that reason).
- **Fetch on attribute access**: a plain attribute read cannot await in async Python, and a hidden query per row is the N+1 ADR-0007 exists to prevent.
- **Read an include that finds no row as `None`**: the declared type is `Team`, not `Team | None`; a missing row is a fact about the data, which the unloaded instance reports when it is read instead of hiding it in the type.

Consequences:

- ADR-0007 and ADR-0008 are superseded in part: the complete-instance invariant has two states, and a relation nothing loaded reads as an unloaded instance, not an awaitable. Projected records, materialization plans and the include rules stand.
- `await player.team` raises Python's own `TypeError`; the fix is `await player.team.load()`. A one-to-one reverse relation is a query like every reverse relation, read with `.first()`.
- `position_of()` treats an unloaded relation as not included.
- A session that reads the forward relation of every row of a large query holds one identity-map entry per distinct target key, the same as an include of that relation would.

Amended (2026-11-09, PR #919): every serialization path refuses an unloaded instance, not only its own `model_dump()` and `model_dump_json()`. A nested model, a `TypeAdapter` and a response model serialize through pydantic-core, which never calls those methods, so the refusal sits in each model's core schema, wrapped around whatever serializer the model declares. pydantic-core wraps any error a serializer raises, so on those paths the refusal arrives as a `PydanticSerializationError` whose message carries the `InstanceNotLoaded` one; in Python mode the `InstanceNotLoaded` is its `__cause__`, and in JSON mode only the message survives. The instance's own `model_dump()` and `model_dump_json()` still raise `InstanceNotLoaded` itself.

Amended 2026-11-09 (Taylor, PR #919): `InstanceNotLoaded` is a `FerroError` and no longer an `AttributeError`. A refusal must never be swallowed as "attribute missing": a Pydantic model built `from_attributes=True` reads each field with `getattr` and treats an `AttributeError` as an absent attribute, so `TeamOut.model_validate(player.team)` took `name`'s default from an unloaded instance (`id=1 name='unknown'`), and a required field failed as "Field required" with no `.load()` fix. That is the partial row this record says an unloaded instance never passes for. Now `hasattr(player.team, "name")` and `getattr(player.team, "name", None)` raise `InstanceNotLoaded` instead of answering `False` and `None`, and a `from_attributes` model fails validation with a `get_attribute_error` whose message carries the refusal. A name that is no column (`_repr_html_`, a typo) still raises a plain `AttributeError`.
