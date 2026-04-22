# Design: Shadow foreign-key column types and forward references

**Date:** 2026-04-22
**Context:** [GitHub issue #16](https://github.com/syn54x/ferro-orm/issues/16) — shadow `{relation}_id` fields are typed as `Union[int, str, None]`, which breaks models whose related primary key is `UUID` or another non-coercible type under Pydantic v2. Forward references on `ForeignKey` targets are common (circular imports); this spec records the agreed behavior and implementation shape.

## Problem statement

1. **Validation:** Assigning a related model instance during `create()` copies the target’s PK into `{name}_id`. If that slot is typed `Union[int, str, None]`, Pydantic does not accept a `UUID` in the same way it would for a `UUID | None` field.
2. **Equality:** Values hydrated from the database may be `str` while the in-memory PK is `UUID`, so strict equality checks fail.
3. **Serialization:** Dumping a model can emit `PydanticSerializationUnexpectedValue` when a `UUID` sits in a `Union[int, str, None]` slot.

Root cause: the shadow column type is **not derived** from the related model’s primary key type; it is hardcoded.

Secondary issue (same theme): `Annotated[Target | None, ForeignKey(...)]` can leave `metadata.to` as a union type, which breaks code paths that assume a model type or forward ref (e.g. `.__name__` in descriptor setup).

## Goals

- Shadow `{relation}_id` annotations should **match the related model’s PK Python type** (plus `None` when the column is nullable in the ORM sense), when that type can be determined.
- **Forward references** must remain ergonomic: unresolved targets at class body time must **not** require a specific import order for basic functionality.
- After `resolve_relationships()` has bound every `ForeignKey.to` to a concrete `Model` subclass, **upgrade** shadow fields that were created with the fallback type so forward-ref-heavy codebases get the same behavior as `Annotated[ConcreteParent, ForeignKey(...)]`.
- Reconcile **nullable relationship annotations** (`Target | None`) so the inner type used for `metadata.to` is always the model (or forward ref), not a `UnionType`.

## Non-goals

- Changing database drivers or Rust hydration formats solely for this feature (coercion should follow Pydantic rules for the chosen Python type, e.g. `str` → `UUID` where applicable).
- Composite primary keys (unless already first-class elsewhere); this spec assumes **one** PK field per model, consistent with existing `Model.__init__` logic that discovers `pk_field` from `ferro_fields`.
- Many-to-many join table column types (still hardcoded integer in join schema in places); separate work if UUID PKs appear there.

## Design

### 1. PK type resolution helper

Introduce a small internal helper (name TBD), e.g. `_pk_python_type_for_model(target: type[Model]) -> Any`, that:

- Iterates `target.ferro_fields` (or equivalent) to find the field with `primary_key=True`.
- Returns that field’s **Python** annotation from `target.model_fields[pk_name].annotation` (or from resolved `__annotations__` if that is the single source of truth in edge cases).

If no PK is found, fall back to today’s conservative union or raise a clear error (match existing product behavior for invalid models).

### 2. Metaclass: `_inject_shadow_fields`

- When `metadata.to` is a **concrete** `Model` subclass (already true for `Annotated[Parent, ForeignKey]` when `Parent` is defined): set
`annotations[id_field] = _optional_pk_type(pk_type)`
(exact optional representation should match current semantics, e.g. `T | None` on supported Python versions).
- When `metadata.to` is **not** resolvable to a concrete model (string, `ForwardRef`, missing): set
`annotations[id_field] = Union[int, str, None]`
(same as today) — **policy A** from brainstorming.

### 3. Metaclass: FK scan and optional `Target | None`

When scanning `Annotated[..., ForeignKey]`, unwrap `args[0]` with the same optional-stripping logic already used for backrefs (`_strip_optional_union`), so:

- `metadata.to` is the model type or forward reference, not `A | None` as a union object.
- Descriptor / name resolution never receives a bare `UnionType` from this path.

### 4. Post-resolution upgrade (`resolve_relationships`)

After existing logic resolves each `rel.to` to a concrete class and mutates registries:

1. **Shadow type reconciliation pass** over `_MODEL_REGISTRY_PY` (or equivalent registry):
  - For each `ForeignKey`, compute `desired = _optional_pk_type(_pk_python_type_for_model(rel.to))`.
  - If the shadow field’s current annotation (or rebuilt core schema) should differ from `desired` — including the case “was fallback union, now target has `UUID` PK” — set `cls.__annotations__[f"{field}_id"] = desired` and mark `cls` for rebuild.
2. For each affected model: `cls.model_rebuild(force=True)` (Pydantic v2).
3. **Re-register Rust schemas** for updated models using the same path as today’s second-pass registration in `resolve_relationships` (or a shared helper), so `model_json_schema()` / `__ferro_schema__` / `register_model_schema` reflect new FK property types.

**Ordering:** One full pass over all models is expected to suffice because PK types come from targets, not from peer children. If tests reveal ordering edge cases, a bounded second pass (“repeat until no model marked dirty”) is acceptable.

**Idempotency:** Safe to call `resolve_relationships()` multiple times (tests); skip or no-op when annotation already matches `desired`.

### 5. Testing expectations

- **Regression:** Existing int/str PK + `ForeignKey` tests and `test_metaclass_internals` expectations for concrete targets must be updated if assertions pin `Union[int, str, None]` where the target PK is known.
- **UUID PK:** Reproducer from issue #16 — `create` with instance, equality after `get`, `model_dump` / `model_dump_json` without serialization warnings.
- **Forward ref:** Model with `Annotated["Other", ForeignKey(...)]` where `Other` is declared later; after import completes and `resolve_relationships()` runs, shadow field annotation should match `Other`’s PK type (e.g. `UUID | None`).
- **Nullable FK annotation:** `Annotated[Parent | None, ForeignKey(...)]` does not crash metaclass or relationship resolution.

## Open questions (resolved for this spec)


| Question                                     | Decision                                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Unresolved `ForeignKey.to` at metaclass time | Fallback `Union[int, str, None]` (policy A).                                              |
| Forward refs after full model graph load     | Upgrade shadow types + `model_rebuild` + schema re-register post-`resolve_relationships`. |


## References

- `src/ferro/metaclass.py` — `_inject_shadow_fields`, `_scan_relationship_annotations`, `_strip_optional_union`
- `src/ferro/relations/__init__.py` — `resolve_relationships`, second-pass schema registration
- `src/ferro/models.py` — `Model.__init__` FK → `{name}_id` assignment
- Issue #16 — symptoms, minimal reproducer, suggested `_get_target_pk_type` direction
