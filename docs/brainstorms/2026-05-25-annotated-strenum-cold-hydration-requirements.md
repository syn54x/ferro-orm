---
date: 2026-05-25
topic: annotated-strenum-cold-hydration
issue: https://github.com/syn54x/ferro-orm/issues/65
---

# Annotated StrEnum Cold Hydration

## Summary

Register enum field types once at model class definition (using the same annotation-unwrapping logic as schema registration), then coerce string values from the database into enum members on every hydration path. This fixes cold fetches for `Annotated[StrEnum, FerroField(db_type="text")]` under PEP 563/649 deferred annotations without duplicating discovery logic in `_fix_types`.

---

## Problem Frame

Ferro hydrates rows through a zero-copy Rust path that populates instance `__dict__` with raw column values. String-valued columns therefore arrive as `str`, not as the declared Python enum. A post-hydration pass (`Model._fix_types`) is responsible for coercing those strings into enum members.

That pass discovers enum fields lazily on first use. Discovery calls `get_type_hints` with the **ferro.models** module namespace, not the user model’s defining module. With `from __future__ import annotations` (common on Python 3.14+), deferred annotation strings never resolve in that namespace; the fallback reads `__annotations__`, which are still strings and do not match `isinstance(hint, type)`. Result: `_enum_fields` stays empty, coercion is skipped, and cold reads return plain `str` even though `__ferro_schema__` already records `enum_type_name` correctly.

`create()` in the same process often still yields enum instances because Pydantic validates on construction. The bug surfaces on **cold** `all()` / `get()` / query results after `reset_engine()` or a fresh connection — exactly when apps rely on `.value`, `match`/`case`, or strict `isinstance` checks.

Equality with the raw string (`instance.mode == "hourly"`) still works; the failure mode is **type fidelity** and enum APIs, not storage or SQL.

---

## Requirements

**Enum registration (canonical, class-definition time)**

- R1. Each concrete `Model` subclass exposes a stable `_enum_fields: dict[str, type[Enum]]` populated during metaclass setup (Phase 3), not on first `_fix_types` call.
- R2. Registration uses the shared `_enum_subclass_from_annotation` helper already used when building `__ferro_schema__`, so `Annotated[...]`, optional unions, and plain enum annotations are handled identically for schema and hydration.
- R3. The source of truth for which fields are enums is Pydantic’s resolved `model_fields[<name>].annotation` (with `get_type_hints(cls, include_extras=True)` as fallback only when a field annotation is missing), never `get_type_hints` with ferro-internal `globals()` / `locals()`.
- R4. `_fix_types` performs **coercion only** against the pre-built `_enum_fields` map; it must not re-scan annotations or mutate discovery state on fetch.

**Hydration behavior**

- R5. After any Rust-backed fetch (`all`, `get`, `fetch_filtered`, query builder `first`/`all`, `ModelConnection.get_or_none`, etc.), every non-null enum column whose stored value is not already an instance of the registered enum class is coerced via `enum_cls(raw_value)` (preserving current tolerant failure behavior for invalid DB values).
- R6. Cold fetch after `reset_engine()` + reconnect must return enum members, not `str`, for models using `Annotated[StrEnum, FerroField(db_type="text")]` with PEP 563/649 deferred annotations.
- R7. Behavior for plain `StrEnum` fields (no `Annotated`), native Postgres enum columns, and `IntEnum` with integer storage remains unchanged — no regression in existing round-trip tests.

**Testing**

- R8. Add an integration regression test that defines a model with `from __future__ import annotations`, `Annotated[StrEnum, FerroField(db_type="text")]`, inserts via `create()`, calls `reset_engine()`, reconnects, fetches via `all()` (or `get`), and asserts `isinstance(field, EnumSubclass)` and `.value` access works.
- R9. Optionally assert `_enum_fields` is non-empty and includes the field name after class creation (unit-level guard against rediscovering the #65 failure mode).

---

## Acceptance Examples

- **AE1 — Cold fetch with deferred annotations**
  Covers: R5, R6, R8
  Given `from __future__ import annotations` and `billing_mode: Annotated[Mode, FerroField(db_type="text")]`, when a row is created with `Mode.HOURLY`, then `reset_engine()` and reconnect, then `Row.all()[0]`, then `type(row.billing_mode)` is `Mode` and `row.billing_mode.value == "hourly"`.

- **AE2 — Schema parity unchanged**
  Covers: R2, R7
  Given the same model class, `__ferro_schema__["properties"]["billing_mode"]["enum_type_name"]` remains `"mode"` (lowercased class name) before and after the fix.

- **AE3 — Invalid DB value tolerance**
  Covers: R5
  Given a text column containing a string that is not a valid enum member, when fetched, the instance field remains a non-enum value (or coercion fails silently as today) without raising during `_fix_types` — no change to defensive behavior unless separately specified.

---

## Success Criteria

- Issue #65 reproduction script exits 0 on main after the fix.
- New regression test fails on current `main` without the fix and passes with it.
- No new phantom DDL or cross-emitter drift (hydration-only change).
- `_enum_fields` discovery logic exists in exactly one conceptual place (metaclass + shared helper), not duplicated in `_fix_types`.

---

## Scope Boundaries

**In scope**

- Python-side enum registration and post-hydration coercion for all existing fetch entry points.
- Regression test under PEP 563/649 + `Annotated` + `db_type="text"`.
- Closes GitHub issue #65.

**Out of scope**

- Rust-side enum coercion during dict population (duplicate logic across FFI; defer unless profiling proves Python coercion is a bottleneck).
- Changing default storage away from native Postgres enums in Alembic (separate product decision; `db_type="text"` on StrEnum remains valid).
- Pydantic `model_validate` on hydration (violates direct-to-dict / zero-copy invariant I-2).
- Broader refactors of `_fix_types` for non-enum types (UUID, Decimal, etc.) unless already planned elsewhere.

---

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where to register enums | Metaclass Phase 3 | Same lifecycle as `ferro_fields` and schema registration; immune to wrong `get_type_hints` namespaces. |
| Shared helper | Reuse `_enum_subclass_from_annotation` | Schema and hydration must agree on what counts as an enum field (AGENTS.md parity spirit). |
| `_fix_types` role | Coercion only | Eliminates lazy discovery bug class; cheaper on hot path. |
| Minimal patch alternative | Repair `_fix_types` discovery only | **Rejected as lesser solve** — fixes #65 but leaves two discovery paths and repeats failure modes for the next annotation shape. |

---

## Dependencies / Assumptions

- Pydantic v2 continues to resolve `model_fields[].annotation` to real enum types even when `__annotations__` on the class remain strings under PEP 563.
- `reset_engine()` remains a valid way to simulate cold identity-map clears in tests.
- Issue reporter environment (ferro 0.10.3, Python 3.14+, SQLite) matches current test matrix support.

---

## Outstanding Questions

- None blocking implementation. If PEP 649-only models without `__annotate_func__` resolution in metaclass appear in the wild, confirm `_resolve_deferred_annotations` still leaves `model_fields` authoritative (spot-check in plan phase).
