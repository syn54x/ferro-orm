---
date: 2026-05-13
topic: configurable-column-storage-types
---

# Configurable Column Storage Types

## Summary

Add an opt-in `db_type=` option to `Field()` that lets users override the SQL column type Ferro emits for a given Python field — most commonly to escape native DB enum types (back to `text` or `int`), but also to pick `BIGINT` over `INT` and `text` over native `UUID`. A companion `db_check=` adds DB-side `CHECK` validation for closed-domain types when native enum storage is declined.

---

## Problem Frame

Ferro currently maps every Python `Enum`/`StrEnum`/`IntEnum` field to a named SQL enum type — `sa.Enum(python_enum, name=...)` on the Alembic side, and a matching `CREATE TYPE` on the Rust runtime side. Native enum types are storage-efficient and self-documenting, but they make schema evolution expensive: adding a variant on Postgres requires `ALTER TYPE ... ADD VALUE` (historically not transactional), renaming or removing a variant requires a multi-step migration with shadow columns, and the same enum is often referenced by multiple tables which complicates `DROP TYPE`. SQLite has no native enum type at all, so the current behavior already diverges across dialects in practice.

The same shape of problem applies to other column types where Ferro's default does not match the user's intent. A user with a counter that will exceed two billion rows wants `BIGINT`, not `INT`. A user who runs on Postgres in production but SQLite locally wants `UUID` columns stored as text so the schema is portable. Today there is no Pythonic way to express those overrides — users either change Pydantic annotations away from their preferred semantic types or hand-edit migrations after Ferro generates them.

The cost is real and recurring: every team that hits the enum-evolution wall, every team that needs `BIGINT`, every team that wants portable UUIDs has to either fight the framework or abandon it. The feature lets the user keep writing standard Pydantic models while picking a column type that fits their operational reality.

---

## Requirements

**Field-level API**

- R1. `Field()` accepts a new `db_type: str | None = None` kwarg that overrides Ferro's default SQL type for that column. The value is a canonical token from a fixed Ferro vocabulary (see R4).
- R2. `Field()` accepts a new `db_check: bool = False` kwarg. When `True`, Ferro emits a DB-side `CHECK` constraint enforcing the allowed values. Only valid when the Python annotation has a finite domain (`enum.Enum` subclasses, `typing.Literal[...]`); any other use raises `TypeError` at class definition.
- R3. Both kwargs flow through the existing `FERRO_FIELD_EXTRA_KEY` plumbing and are reflected in `__ferro_schema__["properties"][col]` so every downstream emitter sees them.

**Canonical vocabulary**

- R4. The canonical `db_type` tokens shipped in this phase are:
  - String/enum storage: `text`, `varchar(N)` where `N` is an integer literal
  - Integer storage: `smallint`, `int`, `bigint`
  - UUID storage: `uuid`, plus `text` and `varchar(N)` as cross-references from the string family
  - Temporal storage: `timestamp`, `timestamptz`, `date`, `time`
- R5. Ferro owns the canonical-token → per-dialect SQL mapping. SQLite's `INTEGER` is the canonical translation for `smallint`, `int`, and `bigint` because SQLite has no fixed-width integer types; the parity test asserts both emitters agree.

**Validation (strict at class definition)**

- R6. Ferro rejects incoherent combinations at metaclass time with `TypeError`. Incoherent combinations include but are not limited to:
  - `db_type="int"`/`"bigint"`/`"smallint"` on a `str`, `StrEnum`, or `UUID` field
  - `db_type="text"`/`"varchar(N)"` on an `int`, `IntEnum`, `datetime`, `date`, or `time` field where Ferro cannot losslessly serialize the Python value
  - `db_type="uuid"` on any field whose Python type is not `UUID`
  - `db_type` set to a token outside the canonical vocabulary
  - `db_check=True` on a field whose annotation is not a closed-domain type (`Enum` subclass or `Literal[...]`)
  - `db_check=True` combined with default (native enum) storage — redundant; native enum already enforces values

**Cross-emitter parity (AGENTS.md I-1)**

- R7. Both the Alembic autogenerate bridge (`src/ferro/migrations/alembic.py`) and the Rust runtime emitter (`src/operations.rs`/`src/schema.rs`) produce byte-identical DDL for the same `(field annotation, db_type, db_check, dialect)` tuple.
- R8. The canonical-token → SQL-type translation table is the single source of truth; both emitters consume it (e.g. a shared static table expressed in both runtimes, with a parity test that walks every `(canonical, dialect)` pair and asserts equality).
- R9. `db_check` constraints follow the existing naming convention `ck_<table>_<col>` and the new artifact is added to the `AGENTS.md` § I-1 table.

**Autogenerate and migrations**

- R10. When `db_type` changes between revisions, Alembic autogenerate produces a real `ALTER COLUMN` migration with an explicit `USING` clause where the dialect requires it (`col::text`, `col::bigint`, etc.).
- R11. When the change drops the last reference to a native DB enum type, autogenerate emits a deferred `DROP TYPE <enum_name>` ordered after every column that uses it has been migrated.
- R12. When `db_check=True` is added or removed, autogenerate emits `ADD CONSTRAINT ck_<table>_<col>` / `DROP CONSTRAINT ck_<table>_<col>` operations with names consistent across both emitters.
- R13. Storage-type changes whose autogenerate output would risk silent data loss or require a full table rewrite (e.g. shrinking `bigint → int` on MySQL) emit a clear warning in the generated migration script that the user can choose to keep or remove.

**Backward compatibility**

- R14. Models that do not set `db_type` or `db_check` produce identical DDL to today. No existing model definition, migration, or test needs modification.

---

## Acceptance Examples

- AE1. **Covers R1, R6, R7.** Given a `StrEnum`-typed `format` field declared with `Field(db_type="text")`, when the schema is emitted on Postgres by either the Alembic bridge or the Rust runtime, the column is `format TEXT NOT NULL` with no `CREATE TYPE` statement, no `CHECK` constraint, and both emitters produce identical SQL.
- AE2. **Covers R2, R9, R12.** Given the same field declared with `Field(db_type="text", db_check=True)`, the emitted DDL includes `format TEXT NOT NULL` and a separate `CHECK (format IN ('pdf','json'))` constraint named `ck_<table>_format`. Removing `db_check=True` between revisions produces an autogenerate diff with a single `DROP CONSTRAINT ck_<table>_format` operation.
- AE3. **Covers R6.** Given a `StrEnum` field declared with `Field(db_type="int")`, importing the module raises `TypeError` with a message naming the field and the conflict between `StrEnum` and integer storage.
- AE4. **Covers R5, R7, R8.** Given an `int` field declared with `Field(db_type="bigint")`, the column emits `BIGINT` on Postgres and MySQL and `INTEGER` on SQLite, and the parity test confirms both emitters agree on each dialect.
- AE5. **Covers R10, R11.** Given a model that previously used native enum storage for `format` and now declares `Field(db_type="text")`, `alembic revision --autogenerate` produces a migration containing `ALTER COLUMN format TYPE TEXT USING format::text` and a deferred `DROP TYPE fileformat` after all columns of that type have been migrated.
- AE6. **Covers R14.** Given every existing test model in the repo, running both emitters before and after this change produces byte-identical DDL.

---

## Success Criteria

- A user with a `StrEnum` field can switch to TEXT storage with a one-line change (`Field(db_type="text")`) and run autogenerate to produce a clean migration off the native enum type.
- A user with a counter column can declare `db_type="bigint"` and trust that Postgres, MySQL, and SQLite all produce the right column without manual migration edits.
- Phantom-diff regressions stay at zero: the existing `test_index_name_matches_rust_runtime_*` parity tests are joined by `test_db_type_matches_rust_runtime_*` covering every canonical token on every supported dialect.
- A downstream agent or implementer can read this document, find the canonical vocabulary in R4, the parity contract in R7-R9, and the autogenerate rules in R10-R13, and proceed to planning without inventing product behavior.

---

## Scope Boundaries

- **Per-dialect dict escape hatch** (`db_type={"postgres": "JSONB", "sqlite": "TEXT"}`) — deferred to Phase 2; ship canonical tokens first and prove the parity model.
- **Canonical vocabulary beyond R4** — `numeric(p,s)`, `char(N)`, `bytea`/`blob`, `jsonb`, array types, and other dialect-specific types are out of scope for Phase 1.
- **Offline data backfills or shadow-column dances** — Ferro's autogenerate output relies on `ALTER COLUMN ... USING`; storage-type changes that need multi-step user-orchestrated migrations remain the user's responsibility.
- **Changing the default for existing models** — models that do not set `db_type` continue to receive today's behavior unchanged.
- **Runtime-level coercion** — `db_type` affects DDL only. Pydantic validation, hydration, and the Rust core's value handling are unchanged.
- **Cross-dialect storage normalization** — Ferro will not silently rewrite values between dialects (e.g. converting `UUID` to lowercase canonical form on read). Users who want that write a Pydantic validator.

---

## Key Decisions

- **API shape is `Field(db_type=..., db_check=...)`, not a new annotation wrapper or model-level config.** Rationale: matches the existing pattern (`primary_key`, `unique`, `index`, `nullable` all live on `Field`), composes with the existing `FERRO_FIELD_EXTRA_KEY` plumbing, and keeps the user's mental model "I am writing Pydantic with extra knobs."
- **Canonical vocabulary, not pass-through SQL strings.** Rationale: Ferro must guarantee cross-emitter parity per AGENTS.md I-1. A canonical vocabulary makes the translation table testable; pass-through SQL would push parity onto users and open the door to dialect-specific breakage. Phase 2 may add an escape hatch on top.
- **Strict validation at metaclass time, no `db_type_force` escape.** Rationale: incoherent combinations are bugs, not preferences. The cost of a clear error at import is much lower than the cost of a silent miscoercion at runtime.
- **`db_check=True` on a default-storage enum field is a `TypeError`, not a no-op.** Rationale: redundancy hides intent. A user who writes both is either confused about what native enum storage does or has a different goal that deserves a clearer API.
- **SQLite's `INTEGER` is the canonical translation for `smallint`, `int`, and `bigint`.** Rationale: SQLite has no fixed-width integer types — `INTEGER` is dynamically sized to hold any 64-bit value. Refusing `bigint` on SQLite would force every cross-dialect user to branch in their model definition, which defeats the point.
- **`uuid → text` on Postgres canonicalizes to `TEXT`, not `VARCHAR(36)` or `CHAR(36)`.** Rationale: `TEXT` is the simplest portable choice and matches Ferro's existing string default. Users who specifically want length-bounded storage can declare `db_type="varchar(36)"` explicitly.
- **`db_check` constraints follow the existing naming convention `ck_<table>_<col>`** and are added to the AGENTS.md § I-1 table as a new tracked artifact.

---

## Dependencies / Assumptions

- The translation table for `(canonical_token, dialect) → SQL type` lives in both the Python and Rust runtimes and is kept in sync by a parity test, following the recipe in `docs/solutions/patterns/cross-emitter-ddl-parity.md`. Planning will decide whether to express it as a shared data file or duplicated constants with a parity assertion.
- A new `docs/solutions/patterns/` entry captures the canonical vocabulary, the translation table, the autogenerate comparator rules for storage-type changes, and the `db_check`-on-closed-domain validation rule. This is institutional memory per AGENTS.md I-5.
- This work assumes the existing `__ferro_schema__` JSON representation can carry both `db_type` and `db_check` without disrupting consumers that ignore unknown keys. Planning verifies this against current consumers.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R8][Technical] Should the canonical-token → dialect-SQL table be expressed as a shared JSON file consumed by both Python and Rust, or as duplicated constants in each runtime with a parity test enforcing equality? The shared-file option is more DRY but adds a new build dependency; duplicated constants follow the existing pattern in `cross-emitter-ddl-parity.md`.
- [Affects R11][Technical] What's the cleanest way to express "deferred `DROP TYPE` ordered after all column migrations" in Alembic autogenerate's output? Alembic operations are normally per-column; this needs a post-pass.
- [Affects R6][Needs research] Are there `Literal[...]` patterns Ferro currently accepts that we have not classified as closed-domain? Planning should grep for `Literal` usage in `_annotation_utils.py` and decide whether `db_check` extends to all of them or only string/int literals.
- [Affects R13][Technical] What's the right surface for the data-loss warning in R13 — a comment in the generated migration script, an Alembic-level warning, or both? The answer depends on what `alembic revision --autogenerate` already does today for similar cases.
