---
title: "feat: Configurable column storage types (db_type / db_check)"
type: feat
status: active
date: 2026-05-13
origin: docs/brainstorms/2026-05-13-configurable-column-storage-types-requirements.md
---

# feat: Configurable column storage types (db_type / db_check)

## Summary

Add two opt-in kwargs to `Field()` — `db_type` (canonical SQL-type override) and `db_check` (closed-domain DB-side validation) — wire them through `build_model_schema` so both emitters see them, add a duplicated canonical-token → dialect-SQL dispatch in the Alembic and Rust paths governed by a new parity test, teach Alembic autogenerate to emit real `ALTER COLUMN` / `DROP TYPE` / `ADD CONSTRAINT` operations when these knobs change, and add `ck_<table>_<col>` to the cross-emitter naming convention.

---

## Problem Frame

Ferro currently forces every `Enum`-typed field into a named DB enum type, which makes schema evolution expensive (`ALTER TYPE ... ADD VALUE`, multi-step renames, `DROP TYPE` ordering). The same shape of problem exists for `int` (want `BIGINT`), `UUID` (want portable `TEXT`), and `datetime` (want `timestamptz` vs `timestamp`). Origin: `docs/brainstorms/2026-05-13-configurable-column-storage-types-requirements.md`.

---

## Requirements

- R1. `Field()` accepts `db_type: str | None = None` and `db_check: bool = False`; both flow through `FERRO_FIELD_EXTRA_KEY`.
- R2. `build_model_schema` propagates `db_type` and `db_check` onto `properties[col]` so every emitter sees them.
- R3. Canonical `db_type` vocabulary: `text`, `varchar(N)`, `smallint`, `int`, `bigint`, `uuid`, `timestamp`, `timestamptz`, `date`, `time`. Tokens outside this set raise `TypeError` at class definition.
- R4. Strict Python-type ↔ `db_type` validation runs at class-definition time; incoherent combinations (see origin R6) raise `TypeError`.
- R5. `db_check=True` is only valid on closed-domain annotations (`enum.Enum` subclass or `typing.Literal[...]`) AND only when `db_type` is also set; combining with default native-enum storage is a `TypeError`.
- R6. Both emitters produce byte-identical DDL for every `(annotation, db_type, db_check, dialect)` tuple. Parity test enforces this.
- R7. SQLite collapses `smallint`/`int`/`bigint` to `INTEGER`; parity test asserts both emitters agree.
- R8. `db_check` constraints are named `ck_<table>_<col>` and added to `_FERRO_NAMING_CONVENTION` (`ck` key) plus the AGENTS.md § I-1 table.
- R9. Alembic autogenerate produces `ALTER COLUMN ... TYPE <new> USING <expr>` when `db_type` changes between revisions, with a dialect-appropriate `USING` clause.
- R10. When the change drops the last reference to a native DB enum type, autogenerate emits `DROP TYPE <enum_name>` ordered after every `ALTER COLUMN` that previously used it.
- R11. Autogenerate emits `ADD CONSTRAINT ck_<table>_<col>` / `DROP CONSTRAINT ck_<table>_<col>` when `db_check` toggles.
- R12. Storage-type changes that risk data loss (e.g. `bigint → int` on MySQL) emit a warning comment in the generated migration script.
- R13. Models that set neither `db_type` nor `db_check` produce DDL identical to today; no existing test or model needs modification.

**Origin acceptance examples:** AE1 (text storage no CREATE TYPE), AE2 (db_check constraint name + diff), AE3 (TypeError on incoherent combo), AE4 (bigint cross-dialect), AE5 (autogenerate ALTER + deferred DROP TYPE), AE6 (existing models unchanged).

---

## Scope Boundaries

- Per-dialect dict escape hatch (`db_type={"postgres": "JSONB"}`) — deferred to Phase 2.
- Canonical vocabulary beyond R3 (`numeric(p,s)`, `char(N)`, `bytea`/`blob`, `jsonb`, arrays) — out of scope.
- Offline data backfills or shadow-column migration choreography — user's responsibility.
- Changing the default for existing models — no behavior change without `db_type` set.
- Runtime value coercion — `db_type` affects DDL only; Pydantic validation and Rust hydration unchanged.

### Deferred to Follow-Up Work

- Per-dialect dict escape hatch (Phase 2 of this feature): separate plan once Phase 1 parity model has shipped and proven.

---

## Context & Research

### Relevant Code and Patterns

- `src/ferro/fields.py` — Pydantic `Field()` wrapper. The `ferro_kwargs` dict is packed into `json_schema_extra[FERRO_FIELD_EXTRA_KEY]`. Two new keys plug in here.
- `src/ferro/schema_metadata.py::build_model_schema` — central enricher that already sets `primary_key`, `unique`, `index`, `ferro_nullable`, `enum_type_name`. New keys `db_type` and `db_check` land on `properties[col]` here so both emitters consume them from the same JSON.
- `src/ferro/metaclass.py::ModelMetaclass` — Phase 3 (Post-Creation Setup) is where field validation can run once `ferro_fields` and resolved annotations are stable. Existing pattern: `_validate_nullable_option`.
- `src/ferro/migrations/alembic.py` — `_FERRO_NAMING_CONVENTION` dict (currently only `"ix"`); `_map_to_sa_type` is the canonical Python-side dispatch. New `db_type` branch sits at the top of `_map_to_sa_type` before the existing enum/format/json-type cascade.
- `src/schema.rs::json_type_to_sea_query_for_backend` and `build_create_table_sqls` — Rust-side dispatch. Reads `col_info` from the enriched JSON schema. New `db_type` branch sits at the top of `build_create_table_sqls`'s per-column loop, before the existing `(json_type, format)` match.
- `src/operations.rs` — autogenerate-time enum detection (`property_is_enum`, `postgres_enum_udt_by_column`). The runtime autogenerate path reads `db_type` from the JSON schema; existing enum UDT detection still applies to in-DB types.

### Institutional Learnings

- `docs/solutions/patterns/cross-emitter-ddl-parity.md` — canonical recipe for adding a new artifact and a new emitter. The plan follows it: pick a name, wire it into both emitters in the same PR, add a parity test, mention in CHANGELOG.
- `docs/solutions/patterns/shadow-fk-columns.md` — precedent for how Ferro adds a synthesized column to the enriched JSON schema and has both emitters consume it.
- AGENTS.md I-1 (cross-emitter DDL parity), I-3 (no `unwrap()` across FFI), I-5 (`docs/solutions/` is institutional memory) all govern this work.

### External References

- Alembic `process_revision_directives` hook — the documented entrypoint for post-processing the `MigrationScript` operation list. This is where the deferred-`DROP TYPE` post-pass lives.
- SQLAlchemy `Enum` type with `create_type=False` — the SA-side knob for "do not emit `CREATE TYPE`". Useful comparison but not directly used; Ferro's path is to choose a different SA column type entirely (`Text`, `BigInteger`, etc.).

---

## Key Technical Decisions

- **Two new kwargs on `Field()`, not a wrapper type.** Matches the existing pattern (`primary_key`, `unique`, `index`, `nullable`) and rides the `FERRO_FIELD_EXTRA_KEY` plumbing without new vocabulary.
- **Canonical-token vocabulary, duplicated dispatch.** A single canonical string (e.g. `"bigint"`) appears in the JSON schema; each emitter has its own static dispatch from token → SQL. A parity test walks every `(token, dialect)` pair and asserts both emitters render identical SQL. This matches `composite_index_name` precedent — duplicated constants enforced by a paired test, not a shared data file. Avoids a new build dependency.
- **Strict validation in metaclass Phase 3.** `ferro_fields` and resolved annotations are stable there, and existing validation (relationships, nullable) already runs in the same phase. Errors surface at class definition, never at query time.
- **`db_check` requires `db_type` to be set.** A `db_check=True` on a default-storage enum field is a `TypeError` because the native enum already enforces values; the user is either confused or wants something the API does not express.
- **Deferred `DROP TYPE` via `process_revision_directives`.** Alembic's documented post-processing hook is fired with the full `MigrationScript`. Ferro registers a hook that scans the op list, identifies orphaned `CREATE TYPE` references (no `Column.type` of that enum remains in target metadata), and inserts `op.execute("DROP TYPE ...")` after the last `ALTER COLUMN` that references the type. Order matters because the same enum may be used by multiple tables.
- **`ck_<table>_<col>` joins the canonical naming table.** Added to `_FERRO_NAMING_CONVENTION` under the `"ck"` key and to the AGENTS.md § I-1 list. Mirrors how `idx_` / `uq_` are governed today.
- **SQLite `bigint` → `INTEGER`.** SQLite has no fixed-width integer types; `INTEGER` is dynamically sized to 64 bits. Silent translation is correct because no user could observe a difference. Parity test pins this.
- **`uuid` → `text` canonicalization picks `TEXT` on Postgres.** Matches Ferro's existing string default. Users who want `VARCHAR(36)` declare it explicitly.

---

## Open Questions

### Resolved During Planning

- **Translation-table location** — duplicated constants per emitter, paired parity test. Same pattern as `composite_index_name`. Avoids a build dep.
- **Deferred `DROP TYPE` mechanism** — Alembic `process_revision_directives` hook.
- **`db_check`-on-default-storage handling** — `TypeError`, not silent no-op.
- **SQLite `bigint`** — silently translate to `INTEGER`, parity test pins it.
- **`uuid → text` Postgres choice** — `TEXT`.

### Deferred to Implementation

- **`Literal[...]` extension surface for `db_check`** — Planning recommends matching the existing closed-domain check used elsewhere in Ferro (search `src/ferro/_annotation_utils.py` for `Literal` handling). If Ferro does not already classify `Literal[...]` as closed-domain, Phase 1 ships `Enum`-only support and `Literal` joins in a follow-up.
- **Exact `USING` clause shape per `(from_type, to_type, dialect)`** — Most are trivial (`USING col::text`, `USING col::bigint`). The full matrix is best discovered by writing the autogenerate tests and seeing what real Alembic output looks like before pinning each case.
- **Warning surface for R12 (data-loss-risk changes)** — Whether the warning is an Alembic-level `warnings.warn` or a comment injected into the generated script (or both) is decided when implementing U6; depends on what `alembic revision --autogenerate` already does for similar shrinking conversions.

---

## Implementation Units

- U1. **Wire `db_type` / `db_check` from `Field()` into the enriched JSON schema**

**Goal:** New kwargs land on `properties[col]` so both emitters see them. No DDL behavior change yet.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `src/ferro/fields.py` — add `db_type` and `db_check` kwargs and overloads; pack into `ferro_kwargs`.
- Modify: `src/ferro/base.py` — extend `FerroField` (or equivalent metadata holder) to carry the new fields.
- Modify: `src/ferro/schema_metadata.py` — in `build_model_schema`, copy `db_type` and `db_check` from `ferro_fields` metadata onto `properties[col]`.
- Modify: `src/ferro/_core.pyi` — update stubs to reflect the new kwargs.
- Test: `tests/test_schema_db_type_metadata.py` — new file.

**Approach:**
- The kwargs are pure pass-through at this layer; no validation yet (that lands in U2).
- Mirror the shape of how `unique`, `index`, and `nullable` already flow from `Field` → `FerroField` → enriched schema.

**Execution note:** Start with a failing test that declares `Field(db_type="text", db_check=True)` and asserts the enriched schema carries both keys on the property.

**Patterns to follow:**
- `src/ferro/schema_metadata.py` lines 93-114 (`for field_name, metadata in getattr(model_cls, "ferro_fields", {}).items()`).
- `src/ferro/fields.py` ferro_kwargs aggregation pattern (lines 468-489).

**Test scenarios:**
- Happy path: `Field(db_type="bigint")` produces `properties["col"]["db_type"] == "bigint"` in `__ferro_schema__`.
- Happy path: `Field(db_type="text", db_check=True)` produces both keys on the property.
- Happy path: omitting both kwargs leaves the property with no `db_type` / `db_check` keys.

**Verification:**
- `uv run pytest tests/test_schema_db_type_metadata.py` passes.
- No existing test changes.

---

- U2. **Strict validation at class-definition time**

**Goal:** Incoherent combinations raise `TypeError` from the metaclass before any query or emission runs.

**Requirements:** R3, R4, R5

**Dependencies:** U1

**Files:**
- Modify: `src/ferro/metaclass.py` — add a `_validate_db_type_options(cls)` step in Phase 3.
- Modify: `src/ferro/exceptions.py` — add a clear error message helper if useful.
- Modify: `src/ferro/_annotation_utils.py` — extend or add helpers that classify an annotation as `Int`-family / `String`-family / `Enum`-subclass / `UUID` / temporal / closed-domain for `Literal`.
- Test: `tests/test_db_type_validation.py` — new file.

**Approach:**
- Build a `CANONICAL_DB_TYPES: frozenset[str]` of allowed tokens, plus a regex for `varchar(N)`.
- Build a compatibility matrix `db_type → set of compatible annotation classes`. Validate each `ferro_fields` entry against the resolved annotation.
- Closed-domain check for `db_check`: `enum.Enum` subclass OR (deferred) `Literal[...]`. If `Literal` classification doesn't already exist in `_annotation_utils.py`, ship `Enum`-only in this unit and defer `Literal` to a follow-up.

**Execution note:** Test-first; the failure modes are the contract.

**Patterns to follow:**
- `src/ferro/base.py::_validate_nullable_option` for the error-raising shape.
- The metaclass already runs validation steps in Phase 3 — slot this in alongside.

**Test scenarios:**
- Error path: `Field(db_type="int")` on a `StrEnum` field raises `TypeError` with a message naming the field and the conflict.
- Error path: `Field(db_type="bigint")` on a `str` field raises.
- Error path: `Field(db_type="uuid")` on a `str` field (non-UUID) raises.
- Error path: `Field(db_type="banana")` raises (outside canonical vocabulary).
- Error path: `Field(db_check=True)` without `db_type` on an enum field raises (redundant with native enum).
- Error path: `Field(db_check=True)` on a plain `int` field raises (not closed-domain).
- Happy path: all of R3's canonical tokens on compatible annotations import cleanly.
- Happy path: `Field(db_type="varchar(255)")` on a `str` field is accepted; `Field(db_type="varchar(notanumber)")` raises.

**Verification:**
- Every error case produces a `TypeError` whose message names the field and the rule violated.
- Existing test suite still imports cleanly (no model in `tests/` accidentally triggers a new error).

---

- U3. **Canonical-token dispatch in the Alembic emitter (Python)**

**Goal:** The Alembic-generated `MetaData` honors `db_type` and `db_check` and produces the right SA column types and `CheckConstraint`s.

**Requirements:** R6 (Python half), R7 (Python half), R8, R13

**Dependencies:** U1

**Files:**
- Modify: `src/ferro/migrations/alembic.py`:
  - Extend `_FERRO_NAMING_CONVENTION` with `"ck": "ck_%(table_name)s_%(column_0_name)s"`.
  - In `_map_to_sa_type`, add a `db_type` branch at the top that dispatches before the existing enum/format cascade.
  - In `_build_sa_table` (or its caller), when `db_check` is present, append a `sa.CheckConstraint(f"{col} IN (...)", name=...)` to the table args using the SA naming convention.
- Test: `tests/test_alembic_db_type.py` — new file.

**Approach:**
- Canonical dispatch table is a module-level `dict[str, Callable[[str | None], sa.types.TypeEngine]]` keyed by token. `varchar(N)` is handled by a small regex.
- Dialect is implicit in SA (Alembic compiles per the bound dialect); the Python emitter does not branch on backend itself — it returns the canonical SA type and SA handles per-dialect rendering. SQLite's `INTEGER` collapse comes for free via SA's mapping.
- `db_check` is rendered as a single-column `sa.CheckConstraint` whose name flows through `_FERRO_NAMING_CONVENTION`.

**Patterns to follow:**
- Existing `_map_to_sa_type` cascade.
- `_FERRO_NAMING_CONVENTION` pattern (lines 19-21).

**Test scenarios:**
- Happy path: `Field(db_type="text")` on a `StrEnum` field renders `format TEXT NOT NULL` on Postgres and no `Enum(...)` SA type.
- Happy path: `Field(db_type="bigint")` on an `int` field renders `BigInteger` (which SA compiles to `BIGINT` on Postgres/MySQL, `INTEGER` on SQLite).
- Happy path: `Field(db_type="timestamptz")` on a `datetime` field renders `DateTime(timezone=True)`.
- Happy path: `Field(db_type="text", db_check=True)` renders a `TEXT` column plus a `CheckConstraint` named `ck_<table>_<col>` with the `IN (...)` clause containing every enum value.
- Integration: covers AE1 (text storage no CREATE TYPE) and AE2 (db_check name).
- Regression: a model with no `db_type` set produces identical DDL to a baseline snapshot recorded before U3.

**Verification:**
- `uv run pytest tests/test_alembic_db_type.py` passes.
- `uv run pytest tests/test_alembic_autogenerate.py` and `tests/test_schema_constraints.py` still pass without modification (R13).

---

- U4. **Canonical-token dispatch in the Rust emitter**

**Goal:** The Rust `build_create_table_sqls` honors `db_type` and `db_check`, with per-dialect rendering for `bigint`/`smallint`.

**Requirements:** R6 (Rust half), R7 (Rust half), R8, R13

**Dependencies:** U1

**Files:**
- Modify: `src/schema.rs`:
  - Add a `db_type` lookup at the top of the per-column loop in `build_create_table_sqls`, before the existing `(json_type, format)` match.
  - Add a `column_db_type_str(col_info)` helper that reads `db_type` from the JSON schema property.
  - Add `apply_db_type_to_column_def(col_def, token, backend)` that dispatches on the canonical token and per-backend renders the right `ColumnDef` shape. SQLite collapses `smallint`/`int`/`bigint` to `integer()`.
  - For `db_check`, emit a `CHECK (col IN (...))` constraint via `sea_query::Index`-style helper or a hand-rolled DDL fragment appended after the create-table SQL. Name follows `ck_<table_lower>_<col>` produced by a new `db_check_constraint_name` helper alongside `composite_index_name`.
- Modify: `src/operations.rs` — `autogenerate` path: when reading the in-memory schema, treat `db_type` as authoritative over `enum_type_name` for column-type comparison.
- Test: `src/schema.rs` Rust unit tests for the new helpers (`cargo test`).

**Approach:**
- Mirror exactly the canonical vocabulary used in U3. Both emitters consume the same JSON schema string `"db_type"`.
- The new `db_check_constraint_name` helper has the same length/truncation guard as `composite_unique_index_name` (63-char limit suffix).
- AGENTS.md I-3 — no `unwrap()`. Unknown tokens reaching the Rust side are a programming error (validated in U2), so map to a clear `PyErr` rather than panic. Test covers this.

**Patterns to follow:**
- `src/schema.rs::json_type_to_sea_query_for_backend` for the dispatch shape.
- `src/schema.rs::composite_index_name` / `composite_unique_index_name` for the name helper shape and truncation rule.

**Test scenarios:**
- Rust unit test: every canonical token renders the expected `ColumnDef::*` call per backend.
- Rust unit test: `db_check_constraint_name("doc", &["format"])` returns `ck_doc_format`; truncation rule fires past 63 chars.
- Rust unit test: invalid `db_type` token surfaces as a clear `PyErr`, not a panic.
- Python integration test: a model with `db_type` declarations produces the same DDL from `internal_create_tables` (Rust) and the alembic `get_metadata()` rendering (via the existing parity test harness).

**Verification:**
- `cargo test` passes.
- `uv run maturin develop && uv run pytest tests/test_db_type_runtime_create.py` (new) passes.

---

- U5. **Cross-emitter parity test for every canonical token**

**Goal:** A single test walks every `(canonical_token, dialect)` pair and asserts the Alembic-rendered DDL fragment equals the Rust-rendered DDL fragment for the column type and (where applicable) the `db_check` constraint.

**Requirements:** R6, R7, R8

**Dependencies:** U3, U4

**Files:**
- Create: `tests/test_db_type_cross_emitter_parity.py`

**Approach:**
- Mirror the structure of `tests/test_alembic_autogenerate.py::test_index_name_matches_rust_runtime_convention_*`.
- Parametrize over canonical tokens × supported dialects (Postgres, SQLite). For each combination: build a one-column model, render the column fragment from both emitters, assert string equality after normalizing whitespace.
- Also assert `ck_<table>_<col>` parity for closed-domain combos.

**Patterns to follow:**
- Existing `test_index_name_matches_rust_runtime_convention_*` tests as the template for "build a model, get both renderings, assert equality".

**Test scenarios:**
- Covers AE4. Parametrized: every token in R3 × every supported dialect.
- Covers AE2's parity slice: `db_check=True` produces identical `ck_<table>_<col>` naming on both sides.
- Negative: a model with no `db_type` produces identical DDL to the pre-feature baseline (R13).

**Verification:**
- Test passes.
- Test fails loudly if either emitter changes its dispatch in isolation.

---

- U6. **Alembic autogenerate: `ALTER COLUMN`, deferred `DROP TYPE`, `ADD/DROP CONSTRAINT`**

**Goal:** Running `alembic revision --autogenerate` against a database whose schema diverges in `db_type` or `db_check` produces a correct, dialect-aware migration.

**Requirements:** R9, R10, R11, R12

**Dependencies:** U3, U5

**Files:**
- Modify: `src/ferro/migrations/alembic.py` — register a `process_revision_directives` hook factory; existing `get_metadata()` already provides the right SA `MetaData`, so type comparison falls out of standard autogenerate. The hook does the post-pass for orphaned `DROP TYPE` and the data-loss warning.
- Test: `tests/test_db_type_autogenerate.py` — new file, follows `tests/test_alembic_autogenerate.py` setup.

**Approach:**
- Standard autogenerate already detects `Column.type` changes between current DB state and target `MetaData`. With U3 in place, switching `Field(db_type="text")` on a previously-enum column makes the SA type change from `sa.Enum` to `sa.Text`, and autogen emits `op.alter_column(...)`. The `USING` clause is provided by a small hook that inspects the from/to types and appends a `postgresql_using=...` kwarg when needed.
- Deferred `DROP TYPE`: the `process_revision_directives` hook walks `MigrationScript.upgrade_ops` after autogenerate has assembled the op list, identifies any SA `Enum` type that is no longer referenced by any column in the target `MetaData`, and appends `op.execute("DROP TYPE <enum_name>")` after the last `ALTER COLUMN` op that referenced it.
- `db_check` toggles surface as `CheckConstraint` additions/removals in the SA `MetaData` between revisions; autogenerate already supports `add_constraint` / `drop_constraint` for named constraints once the `ck` key is in `_FERRO_NAMING_CONVENTION` (U3).
- Data-loss warning (R12): the hook detects narrowing changes (`bigint → int`, `text → varchar(N)` where N is short) and inserts a comment line into the rendered migration script.

**Execution note:** Test-first against the offline autogenerate harness already used by `tests/test_alembic_autogenerate.py`. Do not require a live Postgres for the unit tests; reserve live-DB validation for an integration test if practical.

**Patterns to follow:**
- `tests/test_alembic_autogenerate.py` — existing autogenerate test setup and assertion style.
- Alembic docs on `process_revision_directives` and `MigrationScript`.

**Test scenarios:**
- Covers AE5. Given a model that previously declared `format: FileFormat` (native enum) and now declares `Field(db_type="text")`, autogenerate produces a script containing exactly one `op.alter_column("doc", "format", existing_type=sa.Enum(...), type_=sa.Text(), postgresql_using="format::text")` and a deferred `op.execute("DROP TYPE fileformat")` after that alter.
- Multi-table enum sharing: when two tables reference the same enum and only one changes, autogenerate emits the `alter_column` but does NOT emit `DROP TYPE` (orphan check fails).
- Multi-table enum sharing, both change: emits two `alter_column` ops and one `DROP TYPE` after the second alter.
- `db_check` add: autogenerate emits `op.create_check_constraint("ck_doc_format", "doc", "format IN ('pdf','json')")`.
- `db_check` remove: autogenerate emits `op.drop_constraint("ck_doc_format", "doc", type_="check")`.
- Data-loss warning: `bigint → int` change on Postgres produces a warning comment in the rendered script.
- Regression: a model with no `db_type` changes between revisions produces an empty autogenerate diff (R13).

**Verification:**
- `uv run pytest tests/test_db_type_autogenerate.py` passes.
- Existing `tests/test_alembic_autogenerate.py` still passes unchanged.

---

- U7. **Docs and institutional memory**

**Goal:** AGENTS.md and `docs/solutions/` reflect the new artifact and the new pattern.

**Requirements:** R8 (AGENTS.md side), AGENTS.md I-5 compliance

**Dependencies:** U6

**Files:**
- Modify: `AGENTS.md` — add `ck_<table>_<col>` to the numbered list under § I-1, and add U6's `process_revision_directives` hook to the emitter list.
- Modify: `docs/solutions/patterns/cross-emitter-ddl-parity.md` — extend the canonical-names table with the `ck_` row.
- Create: `docs/solutions/patterns/configurable-column-storage-types.md` — capture the canonical vocabulary (R3), the duplicated-dispatch parity strategy (Key Decision 2), the strict-validation rule (R4), the `db_check` closed-domain rule (R5), the deferred-`DROP TYPE` mechanism (R10), and the recipe for adding a new canonical token in Phase 2.
- Modify: `CHANGELOG.md` — entry under the next release covering `db_type` / `db_check`.

**Approach:**
- The pattern doc is the future-agent's first stop when extending the vocabulary (Phase 2) or adding a new emitter. Treat it as load-bearing institutional memory per AGENTS.md I-5.

**Test expectation:** none — documentation-only unit.

**Verification:**
- Markdown lint clean.
- AGENTS.md numbered list reflects the new artifact and emitter entry.

---

## System-Wide Impact

- **Interaction graph:** `Field` → `FerroField` metadata → `build_model_schema` → both emitters. The metaclass validation step joins the existing post-creation validation chain.
- **Error propagation:** strict validation surfaces as `TypeError` at class definition; emitter-level unknown-token errors surface as `PyErr` from Rust (per AGENTS.md I-3) and `ValueError`/`TypeError` from Python.
- **State lifecycle risks:** Alembic autogenerate's `DROP TYPE` post-pass must be idempotent against multiple invocations and must not emit `DROP TYPE` when even one column still references the enum. U6's tests cover this.
- **API surface parity:** `Field` is the single user-facing surface. No new top-level symbols.
- **Integration coverage:** U5 (parity test) and U6 (autogenerate) are the load-bearing cross-layer scenarios.
- **Unchanged invariants:** Every existing model definition continues to produce identical DDL; the metaclass continues to inject `FieldProxy` instances; the Rust hydration path is untouched.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Duplicated dispatch in Python and Rust drifts silently. | U5 parity test fails loudly on any single-side change. Same recipe as `composite_index_name`. |
| Alembic `process_revision_directives` hook interacts poorly with user-registered hooks. | Hook composes with `chain=True` semantics; documented in U7's pattern doc. Test covers the case where a user has their own hook registered. |
| `Literal[...]` closed-domain support is more involved than expected. | Ship `Enum`-only `db_check` in this plan; `Literal` joins in a follow-up. Origin's R6 already allows this scoping. |
| `USING` clause is dialect-specific and we under-cover the matrix. | U6 ships the obvious cases (enum→text, int→bigint); rare conversions surface as autogenerate diffs the user can refine. R12's data-loss warning is the safety net. |
| `db_check` constraints conflict with Pydantic's looser runtime validation when stored values drift. | Documented in U7 pattern doc as expected behavior; constraint is the DB-side last line of defense, not a substitute for Pydantic validation. |

---

## Documentation / Operational Notes

- U7 produces the user-facing reference and the institutional-memory pattern doc.
- CHANGELOG entry should call out that the default behavior is unchanged (R13).
- Worth a docs page snippet showing the three canonical use cases from origin (enum → text, int → bigint, uuid → text).

---

## Sources & References

- **Origin document:** `docs/brainstorms/2026-05-13-configurable-column-storage-types-requirements.md`
- Cross-emitter parity recipe: `docs/solutions/patterns/cross-emitter-ddl-parity.md`
- AGENTS.md § I-1 (cross-emitter DDL parity), § I-3 (no unwrap across FFI), § I-5 (docs/solutions/ as memory)
- Related code: `src/ferro/fields.py`, `src/ferro/schema_metadata.py`, `src/ferro/metaclass.py`, `src/ferro/migrations/alembic.py`, `src/schema.rs`, `src/operations.rs`
- Alembic `process_revision_directives` documentation
