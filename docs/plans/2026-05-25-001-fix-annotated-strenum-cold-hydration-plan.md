---
title: "fix: Annotated StrEnum cold hydration (#65)"
type: fix
status: completed
date: 2026-05-25
origin: docs/brainstorms/2026-05-25-annotated-strenum-cold-hydration-requirements.md
issue: https://github.com/syn54x/ferro-orm/issues/65
---

# fix: Annotated StrEnum cold hydration (#65)

## Summary

Populate `Model._enum_fields` at class-definition time in `ModelMetaclass` using Pydantic’s resolved field annotations and `_enum_subclass_from_annotation`, then reduce `Model._fix_types` to coercion-only. Add a regression test that reproduces issue #65 (`from __future__ import annotations`, cold fetch after `reset_engine()`). Closes the gap where schema registration knows about enums but hydration does not.

---

## Problem Frame

Cold Rust hydration leaves text-backed enum columns as `str`. `Model._fix_types` should coerce them back to enum members, but its lazy discovery uses `get_type_hints(cls, globalns=globals(), localns=locals())` inside `ferro.models` — the wrong namespace. Under PEP 563 (`from __future__ import annotations`), that raises `NameError` for `Annotated`, falls back to string `__annotations__`, and never populates `_enum_fields`. Schema registration already succeeds because `build_model_schema` resolves hints against the model’s defining module and uses `_enum_subclass_from_annotation`.

Origin: `docs/brainstorms/2026-05-25-annotated-strenum-cold-hydration-requirements.md` (issue #65).

---

## Requirements Traceability

| ID | Requirement | Plan unit |
|----|-------------|-----------|
| R1 | Stable `_enum_fields` at class definition | U1 |
| R2 | Shared `_enum_subclass_from_annotation` | U1 |
| R3 | Source: `model_fields[].annotation` (+ hints fallback) | U1 |
| R4 | `_fix_types` coercion-only | U2 |
| R5 | All fetch paths coerce | U2 (verify call sites) |
| R6 | Cold fetch + PEP 563/649 | U3 |
| R7 | No regression on existing enum tests | U3 |
| R8 | New integration regression | U3 |
| R9 | Optional `_enum_fields` unit guard | U3 |

**Acceptance examples:** AE1 (cold fetch), AE2 (schema unchanged), AE3 (invalid DB value tolerance unchanged).

---

## Scope Boundaries

- Python registration + `_fix_types` only; no Rust hydration changes.
- No Pydantic `model_validate` on fetch (I-2 zero-copy hydration).
- No changes to DDL / `enum_type_name` emission (already correct).

**Rejected lesser approach:** Patching only `_fix_types` to call `get_type_hints(cls, include_extras=True)` without ferro `globals()` — fixes #65 but preserves duplicate discovery and drifts from schema logic. Document in PR if useful; do not implement unless explicitly requested.

---

## Context & Research

### Root cause (verified)

Reproduction on `main`:

```text
# with from __future__ import annotations
billing_mode type: str
Row._enum_fields: {}
```

`get_type_hints(Row, globalns=ferro.models.__dict__)` → `NameError: name 'Annotated' is not defined`.  
`get_type_hints(Row, include_extras=True)` (default module) → resolves `Mode`.  
`Row.model_fields['billing_mode'].annotation` → `<enum 'Mode'>` even when `__annotations__` is a string.

### Call sites that invoke `_fix_types` (must remain covered)

- `src/ferro/models.py`: `all`, `get`, instance method path, `ModelConnection.get_or_none`
- `src/ferro/query/builder.py`: query result hydration

No new call sites required if coercion map is populated at import.

### Patterns to follow

- `src/ferro/schema_metadata.py` — `_enum_subclass_from_annotation`, `build_model_schema` enum loop (lines ~144–159)
- `src/ferro/metaclass.py` — Phase 3 post-creation hooks (`_validate_db_type_options` already uses `get_type_hints(cls, include_extras=True)`)
- `tests/test_schema_enum_annotations.py` — schema-only coverage for deferred annotations; extend or sibling file for hydration

### Institutional learnings

- `docs/solutions/issues/pydantic-slots-missing-after-ferro-hydration.md` — hydration path must stay observationally equivalent to Pydantic instances; enum coercion is separate but same “post-Rust normalization” layer.
- AGENTS.md I-2 — do not route hydration through `Model.__init__`.

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Registration timing | Metaclass Phase 3, before/after schema generation | Same lifecycle as `ferro_fields`; map available before any fetch. |
| Annotation source | `cls.model_fields` first | Already resolved by Pydantic; works for PEP 563 string `__annotations__`. |
| Helper | Import `_enum_subclass_from_annotation` from `schema_metadata` | Single definition for schema + hydration (R2). |
| `_fix_types` | Remove lazy discovery block; assume `_enum_fields` exists | `Model` base can set `_enum_fields = {}`; subclasses override at definition. |
| Test placement | New `tests/test_enum_cold_hydration.py` | Keeps `test_schema_enum_annotations.py` schema-focused; cold path is integration-shaped. |

---

## Open Questions

### Resolved

- **Why does schema work but hydration fails?** Different `get_type_hints` namespaces and fallback to raw string annotations.
- **Is Rust coercion needed?** No for this fix; Python post-pass is established pattern and cheap relative to FFI complexity.

### Deferred to implementation

- Whether to initialize `Model._enum_fields = {}` on the base `Model` class explicitly (recommended for `hasattr` simplification).

---

## Implementation Units

### U1. Register `_enum_fields` in metaclass

**Goal:** Every concrete model class has `cls._enum_fields: dict[str, type[Enum]]` before any query runs.

**Requirements:** R1, R2, R3.

**Dependencies:** None.

**Files:**

- Modify: `src/ferro/metaclass.py`
- Optional import: `src/ferro/schema_metadata.py` (existing `_enum_subclass_from_annotation`)

**Approach:**

1. Add `@staticmethod def _register_enum_fields(cls) -> None` on `ModelMetaclass`.
2. Iterate `cls.model_fields.items()`; for each `field_name`, `annotation = finfo.annotation`.
3. Optional fallback per field: if annotation is a `str` or unresolved, try `get_type_hints(cls, include_extras=True).get(field_name)` (same pattern as `_validate_db_type_options`).
4. `enum_cls = _enum_subclass_from_annotation(annotation)`; if not `None`, add to local dict.
5. Assign `cls._enum_fields = mapping` (empty dict when no enums).
6. Call from `__new__` Phase 3 after `super().__new__` and before or after `_generate_and_register_schema` (order irrelevant; both need resolved `model_fields`).

**Test scenarios (U3):**

- After class body definition with `from __future__ import annotations` and `Annotated[StrEnum, FerroField(...)]`, `ModelSubclass._enum_fields` contains the field name and enum class.

---

### U2. Simplify `_fix_types` to coercion-only

**Goal:** Remove broken lazy discovery; coerce using pre-built map.

**Requirements:** R4, R5.

**Dependencies:** U1.

**Files:**

- Modify: `src/ferro/models.py`

**Approach:**

1. On base `Model`, set class attribute `_enum_fields: ClassVar[dict[str, type[Enum]]] = {}` (or document that only subclasses get populated).
2. Replace `_fix_types` body:
   - Remove `if not hasattr(cls, "_enum_fields")` discovery block entirely.
   - Loop `for field_name, enum_cls in cls._enum_fields.items():` with existing coercion (`enum_cls(val)` on non-enum non-None values).
3. Grep for `_enum_fields` mutations elsewhere; there should be none after U1.

**Test scenarios:**

- Covered by U3 integration test (exercises `all()` → `_fix_types`).

---

### U3. Regression tests

**Goal:** Fail on current `main`; pass after U1+U2. Guard AE2/AE3.

**Requirements:** R6–R9, AE1–AE3.

**Dependencies:** U1, U2.

**Files:**

- Create: `tests/test_enum_cold_hydration.py`

**Approach:**

1. **AE1 / R6 / R8** — `test_annotated_strenum_text_cold_fetch_after_reset_engine(db_url)`:
   - Module-level `from __future__ import annotations`.
   - Inner model: `billing_mode: Annotated[Mode, FerroField(db_type="text")]`.
   - `connect`, `create` with enum member, `reset_engine`, `connect`, `all()[0]`.
   - Assert `isinstance(..., Mode)`, `.value == "hourly"`.
2. **AE2 / R7** — In same test or sibling: assert `__ferro_schema__["properties"]["billing_mode"]["enum_type_name"] == "mode"`.
3. **R9** — `test_enum_fields_populated_for_deferred_annotations`: after class definition, `assert Model._enum_fields["billing_mode"] is Mode` (no DB).
4. Run existing enum-related tests: `tests/test_db_type_integration.py::test_strenum_text_storage_round_trip`, `tests/test_schema_enum_annotations.py`, `tests/test_structural_types.py` enum cases.

**Execution posture:** Test-first — run new test before U1/U2 to confirm failure mode (`str`, empty `_enum_fields`).

---

## Sequencing

```text
U3 (write failing test) → U1 (metaclass registration) → U2 (_fix_types) → U3 (green) → full pytest enum subset
```

---

## Verification Checklist

- [ ] `uv run pytest tests/test_enum_cold_hydration.py -q`
- [ ] Issue #65 repro script (inline or committed under `tests/` / `scripts/`) exits 0
- [ ] `uv run pytest tests/test_schema_enum_annotations.py tests/test_db_type_integration.py -k enum -q` (or full file if fast)
- [ ] No `get_type_hints(..., globalns=globals())` left in `_fix_types`
- [ ] PR body: `Fixes #65`

---

## Risks

| Risk | Mitigation |
|------|------------|
| Models defined before imports complete | Same as today for schema; `model_fields` is authoritative. |
| Circular import metaclass ↔ schema_metadata | Already imports `build_model_schema`; adding enum helper is safe. |
| Subclass redefines fields dynamically | Out of scope; Ferro models are static class definitions. |

---

## Handoff to implementation

Use `ce-work` or manual implementation following unit order above. Estimated touch surface: ~40 lines metaclass, ~25 lines models, ~60 lines test.
