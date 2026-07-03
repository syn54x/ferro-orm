# FF-E — Registry & model identity: design

Closes findings **F9** (bare-class-name registry keys silently clobber; table
name not configurable) and **F11** (O(N²) import-time SchemaIR recompiles;
`resolve_relationships` swallows schema failures). Roadmap: Epic FF-E,
`docs/plans/2026-07-02-001-fable-fixes-roadmap.md` L287–318.

Ships in the in-development minor (0.13.0-dev → released as 0.14.0 via
`feat(ff-e)!` commits).

## The problem, concretely

Today three independently-diverging naming schemes exist for one model:

1. **Registry key** — the bare class name. `app.models.User` and
   `admin.models.User` both write `_MODEL_REGISTRY_PY["User"]` (Python,
   `metaclass.py`) and `MODEL_REGISTRY["User"]` (Rust, `schema.rs`); the
   second definition silently clobbers the first.
2. **Table name** — `classname.lower()`, re-derived at ~17 call sites
   (11 CRUD sites + legacy query path in `operations.rs`, 3 in `migrate.rs`,
   2 in `schema.rs`, plus `ir/compiler.py`), never configurable.
3. **FK `to_table` strings** — `target.__name__.lower()`, hand-built in
   `schema_metadata._target_table_name` and the M2M join-table schemas in
   `relations/__init__.py`.

And `resolve_relationships`' second pass wraps every re-registration in
`except Exception: pass`, so a model that fails to rebuild is silently left
on its pre-relationship schema.

## The five decisions

### D1 — Collision key and the fate of same-name re-definition

**Duplicate detection keys on the resolved table name, not the class name.**
The registry key (decision D2) is unique by construction, so it can never
collide; what can collide is two distinct models claiming one physical table.

The rule:

- **Same qualified identity re-registering is idempotent** (latest definition
  wins, exactly like today). Identity = `f"{cls.__module__}.{cls.__qualname__}"`.
  This keeps module re-import (same module + qualname) and interactive/REPL
  redefinition working with zero spurious errors.
- **Two distinct identities resolving to the same table name is a hard error
  at class definition**, raised as `RuntimeError` with both candidates'
  qualified names and the fix:

  ```
  Ferro model 'admin.models.User' resolves to table 'user', which is already
  registered by model 'app.models.User'. Two distinct models cannot share a
  table. Set __ferro_table__ on one of them to give it a distinct table name.
  ```

The check is a scan of `_MODEL_REGISTRY_PY` values comparing resolved table
names — no new global index to keep in sync with the many test fixtures that
snapshot/clear `_MODEL_REGISTRY_PY` directly. The scan is O(N) per class
definition; the expensive per-model cost (schema builds, E3's budget test)
stays O(1) per class.

`tests/test_models.py::test_duplicate_model_registration` is redesigned to
encode this contract explicitly: same-identity redefinition is allowed and
the second definition wins; a companion test asserts two distinct models →
one table is a definition-time error naming both.

**Test-suite consequence (part of this change, not incidental fallout):**
the suite currently never isolates the global registry — function-local test
models accumulate in `_MODEL_REGISTRY_PY` for the whole session, so dozens of
same-named local models (28 `Doc`s, 11 `User`s, …) would collide at
definition once detection lands. The per-file snapshot/restore fixtures some
files already carry become a **global autouse fixture in `tests/conftest.py`**
(snapshot/restore `_MODEL_REGISTRY_PY`, `_PENDING_RELATIONS`,
`_JOIN_TABLE_REGISTRY`), which preserves module-scope models (they are in
the baseline snapshot) while function-local models no longer leak across
tests. The three module-scope duplicate names across test files (`User`,
`Post`, `Product`) are renamed — they are exactly the F9 silent-clobber
pattern the epic exists to make loud.

Additionally, `resolve_relationships` raises if a computed M2M join-table
name collides with a registered model's table (derived-name edge; loud
beats a silent wrong-table CREATE).

### D2 — The registry key on both sides of the FFI

**Both registries key by the qualified identity `module.qualname`; the table
name lives on the registration value, never in the key.**

- The metaclass stamps two class attributes at definition time:
  - `cls.__ferro_identity__: str` — `f"{module}.{qualname}"`, the registry
    key on both sides of the FFI.
  - `cls.__ferro_table__: str` — the **resolved** physical table name
    (configured value or `classname.lower()` default; see D4).
- Python `_MODEL_REGISTRY_PY` keys by `__ferro_identity__`.
- Rust `MODEL_REGISTRY` keys by the same string; `RegisteredModel` gains a
  `table_name: String` field (it already holds schema + codec plan — FF-C's
  "one value per registration" invariant extends naturally).
- `register_model_schema` FFI becomes `(name, schema, table_name)` — three
  required args, no default. Every caller (metaclass, relationship second
  pass, `_reregister_ferro`, join-table registration) passes the resolved
  table explicitly. Join tables pass their table name as both key and table.
- Every Python→Rust name argument (`save_record`, `update_record`,
  `evict_instance`, `register_instance`, QueryIR `model_name`, …) switches
  from `cls.__name__` to `cls.__ferro_identity__`; Rust ops that receive the
  class object extract `__ferro_identity__` instead of `__name__`. Identity-map
  keys and "Model '…' not found" errors carry the qualified name — strictly
  more actionable.
- SchemaIR's `model_name` field carries the qualified identity; `table_name`
  (already in the IR) carries the physical table. Producer and consumer ship
  in one wheel and no IR is persisted, so `ir_version` stays 1.

**FK string resolution** goes through one new function,
`ferro.state.resolve_model_reference(ref)`:

1. exact qualified-key hit → that model;
2. else match `cls.__name__ == ref` across the registry: exactly one → that
   model; several → `RuntimeError` listing the candidates' qualified names
   ("use the qualified name to disambiguate"); none → the existing
   "not found" error.

`resolve_relationships`, both descriptor classes, and
`schema_metadata._target_table_name` all resolve strings through it (the
schema-metadata path uses a non-raising variant during the provisional first
pass — see D3).

### D3 — How the resolved table name reaches every derivation site

**RouteHandle-style: resolve once, read everywhere.** The resolved table name
is stored on the Rust `RegisteredModel` at registration; every
`name.to_lowercase()` becomes a registry read. No FFI operation signature
grows a `table_name` parameter.

Site-by-site:

- `src/operations.rs` — all 11 CRUD sites replace
  `let table_name = name.to_lowercase();` with the `table_name` from the
  registration they already fetch. The legacy query path (~:2947) reads
  `query_def.registration.table_name` and errors loudly when the model is
  not registered (no lowercase fallback).
- `src/migrate.rs` — the runtime migrate loop reads `model.table_name` from
  the registration; the test-support FFI planners take the table name as the
  parameter directly (they are `_for_test` surfaces; callers updated).
- `src/schema.rs` — `order_schemas_for_creation` compares FK `to_table`
  dependencies against registrations' `table_name` instead of lowercased
  keys. `_render_create_table_sql_for_test` keeps matching by
  `model_name` or `table_name`.
- `src/ferro/ir/compiler.py` — `compile_model_schema_ir` passes
  `table_name=model_cls.__ferro_table__` into `compile_schema_ir_payload`;
  the `or model_name.lower()` default remains only for the join-table path,
  which already passes `table_name=` explicitly.
- `src/ferro/relations/__init__.py` — the M2M join table becomes
  `f"{source_table}_{field_name}"`, and `source_col`/`target_col`/`to_table`
  derive from each side's `__ferro_table__`. For default-named models these
  strings are byte-identical to today's.
- `src/ferro/schema_metadata.py` — `_target_table_name` resolves class
  targets via `target.__ferro_table__`; string/ForwardRef targets try
  `resolve_model_reference` (non-raising) and fall back to `ref.lower()`
  only for the provisional first-pass schema, which the relationship second
  pass (now loud, E4) always overwrites before any DDL consumer runs.

Grep gate: after E2, no `to_lowercase()` / `.lower()` **table derivation**
survives outside `ir/compiler.py`'s single resolution point and the
documented provisional fallback in `_target_table_name`.

### D4 — E2's config surface

**`__ferro_table__` ClassVar dunder**, matching the existing
`__ferro_composite_uniques__` / `__ferro_composite_indexes__` precedent —
not a `model_config` key (Pydantic's `ConfigDict` is typed/closed; stuffing
vendor keys in is fighting the framework, and the ClassVar reads at class
scope exactly like the composite-constraint dunders users already know).

```python
class User(Model):
    __ferro_table__: ClassVar[str] = "app_users"
    id: int | None = None
```

Rules:

- Honored only when declared **in the class's own body** (checked against the
  metaclass namespace, not inherited lookup) — a subclass never silently
  inherits its parent's physical table.
- Validated at class definition: must be a `str` (else `TypeError`) matching
  `^[A-Za-z_][A-Za-z0-9_]*$` and ≤63 chars (else `ValueError`).
- After resolution the metaclass stamps `cls.__ferro_table__` with the
  resolved value, so it is also the **read** surface — one dunder, config in,
  truth out. `Model` documents it; the models/schema guide gets both
  Assignment + Annotated tab examples (I-8).

### D5 — E3: deletion, not a new cache layer

**Delete the per-class `compile_registry_schema_ir()` call in
`_generate_and_register_schema`; keep the per-model
`compile_model_schema_ir`.** No fingerprint-reuse layer in
`compile_registry_schema_ir`.

Why deletion alone is the right long-term shape, not a fallback: the three
lazy entry points (`connect`/`create_tables`/`migrate`) already
unconditionally run `resolve_relationships()` + full
`compile_registry_schema_ir()`, and relationship resolution mutates model
schemas *after* class definition — so any definition-time cache of the full
modelset is invalidated by design before first use. A dirty-flag/fingerprint
reuse scheme would add mutable cross-module state to shave an O(N) pass that
runs a handful of times per process (per connect/migrate), while the actual
F11 cost — O(N) full recompiles at *import*, once per class — disappears
entirely with the deletion. Import cost: N schema builds for N models
(one `build_model_schema` + one per-model IR compile each), verified by an
instrumented budget test (count `build_model_schema` calls over a 200-model
`type(...)`-built fixture; assert ≤ c·N, not wall-clock).

## E4 — loud relationship re-registration

The second pass in `resolve_relationships` becomes:

```python
for model_key, model_cls in _MODEL_REGISTRY_PY.items():
    try:
        schema = build_model_schema(model_cls)
    except Exception as exc:
        raise RuntimeError(
            f"Ferro failed to rebuild the schema for model '{model_key}' "
            f"while resolving relationships: {exc}"
        ) from exc
    register_model_schema(model_key, json.dumps(schema), model_cls.__ferro_table__)
```

`register_model_schema` errors propagate unwrapped (they are already
actionable `PyErr`s). Matches the first-pass loop, which has always raised.

## User-observable breaking changes (the `!` list)

1. **Two distinct models resolving to one table name now raise at class
   definition** (was: silent registry clobber). Same-identity redefinition
   stays allowed.
2. **Registry keys are qualified names** on both sides: identity-map keys,
   Rust error messages, QueryIR/SchemaIR `model_name`, and the
   `register_model_schema` FFI signature all change. Private surfaces, but
   anyone touching `_MODEL_REGISTRY_PY` or the FFI directly must adapt.
3. `test_duplicate_model_registration`'s contract is re-specified per D1.

New (non-breaking): `__ferro_table__` configurable table name; M2M artifacts
follow the participants' configured tables (byte-identical for default-named
models).

## Exit-gate tests

- Duplicate table name: two distinct models (different modules/qualnames) →
  definition-time error naming both candidates.
- Same-model idempotency: redefinition under the same qualified identity does
  not error; latest definition wins.
- Custom table name round-trips through both emitters (SQLite + Postgres
  parity, in `tests/test_naming_single_source.py`), including a relation to a
  custom-table-named model pointing at the right `to_table`, and M2M join
  artifacts following custom tables.
- FK short-name resolution: unambiguous short name resolves; ambiguous short
  name errors listing qualified candidates.
- Import budget: 200-model fixture → O(N) `build_model_schema` calls
  (instrumented count, not wall-clock).
- E4: a model whose schema rebuild raises aborts `resolve_relationships`
  with the model named.

## Build order

E4 (isolated warm-up) → E2 table-name authority (stamp `__ferro_identity__`/
`__ferro_table__`, thread `table_name` through the FFI + all ~17 sites) →
E1 qualified keys + collision detection on top → E3 (delete the O(N²) call +
budget test). Separate conventional commits, scope `ff-e`, `!` on E1 and the
key-scheme change.
