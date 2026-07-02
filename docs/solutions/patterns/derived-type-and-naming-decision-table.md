---
title: Derived-type & naming decision table (FF-B)
type: pattern
tags: [convention, invariant, schema, migrations, enum, naming, alembic, sea-query]
related_files:
  - AGENTS.md
  - crates/ferro-ddl-lowering/src/lib.rs
  - src/ferro/migrations/alembic.py
  - src/ferro/ir/compiler.py
  - crates/ferro-migrate/src/emit.rs
related_issues: [141, 154]
captured: 2026-07-02
---

## Problem

I-1 requires byte-identical DDL from the Rust and Alembic emitters, but two
vocabularies were still multi-sourced: **derived type lowering** (what storage a
field gets when the user sets no `db_type`) and **artifact naming**. Each side
kept its own copy, and the copies drifted — a plain `datetime` field lowered to
`timestamptz` from Rust but timezone-naive `sa.DateTime()` from the bridge; an
`Enum` field lowered to a native Postgres enum from the bridge but `varchar`
from Rust; Python's single-column name helpers had no 63-char truncation guard
while Rust's did. Every drift is a phantom diff (or worse, a wrong column type)
on Postgres, where the sqlite-only parity sentinel could not see it.

## Takeaway

**One decision table per domain, one owner, every emitter consumes it.**
Derived type lowering is decided by `ferro_ddl_lowering::canonical_from_parts`
(scalar cascade) plus enum resolution (`resolve_column_storage`, FF-B B2);
artifact names are built only by the `ferro-ddl-lowering` helpers, exposed to
Python over `_core` FFI. Parity tests demote to regression sentinels over the
shared code — nothing re-implements the rules.

## The derived-type decision table

`db_type` (explicit) always wins, via `db_type_token_to_canonical`. Otherwise
`(logical_type, format)` decides:

| `(logical_type, format)`        | Canonical storage | Postgres           | SQLite declared type |
| ------------------------------- | ----------------- | ------------------ | -------------------- |
| `("string", "date-time")`       | `TimestampTz`     | `timestamptz`      | `DATETIME`           |
| `("datetime", _)`               | `TimestampTz`     | `timestamptz`      | `DATETIME`           |
| `("string", "date")` / `("date", _)` | `Date`       | `date`             | `DATE`               |
| `("time", _)` / `("string", "time")` | `Time` *(FF-B: was `Varchar`)* | `time` | `TIME`     |
| `("string", "uuid")` / `("uuid", _)` | `Uuid`       | `uuid`             | `CHAR(32)`           |
| `(_, "decimal")` / `("decimal", _)` | `Decimal`     | `numeric`          | `NUMERIC`            |
| `("string", "binary")` / `("binary", _)` | `Blob`   | `bytea`            | `blob`               |
| `("json" \| "object" \| "array", _)` | `Json`       | `json`             | `JSON`               |
| `("integer", _)`                | `Integer`         | `integer`          | `integer`            |
| `("number", _)`                 | `Double`          | `double precision` | `double`             |
| `("boolean", _)`                | `Integer` (SQLite) / `Boolean` (PG) | `boolean` | `integer`  |
| `("string", _)`                 | `Varchar(None)`   | `varchar`          | `varchar`            |
| unknown                         | error at the FFI boundary — never a silent varchar fallback |

The SQLite declared spellings are **SQLAlchemy's** (FF-B B5): sea-query's
`*_text` defaults (`timestamp_with_timezone_text`, `uuid_text`, `json_text`,
`date_text`, `time_text`, `real` for decimal) reflected as bare `TEXT`/`REAL`
under SQLAlchemy and produced phantom `modify_type` diffs in the sentinel.
SQLite's type affinity makes the storage classes identical either way, so
existing databases keep working (the affinity-class comparison in
`sqlite_type_storage_drift` treats both spellings as equivalent). PK columns
also carry an explicit `NOT NULL` — SQLite's PRAGMA otherwise reports an
`INTEGER PRIMARY KEY` as nullable, which read back as a phantom nullability
diff; the IR compiler clamps PK `nullable` to `false` accordingly.

**Enum fields** (IR `enum_values` present, no explicit `db_type`) are not a
scalar row: on **Postgres** they lower to a **native enum type** named
`enum_type_name` (the Python enum class name, lowercased; falls back to the
column name), created idempotently before the table; on **SQLite** they lower
to `varchar(max label length)` — byte-matching what SQLAlchemy renders for
`sa.Enum(*labels, name=...)` on each backend. The runtime CRUD path already
speaks native enum (bind casts, text-cast projection); FF-B makes the Rust
*emitter* match the bridge, which already emitted `sa.Enum`. Rationale for
native enum as the default: the metaclass has always treated it as the
contract (`db_check` without `db_type` is rejected as "redundant on default
(native enum) storage"), and the native type enforces the closed domain in the
database itself. `db_check` therefore only exists for explicit scalar
`db_type` overrides.

Int-enum labels are **stringified** (`{"1", "2", "3"}`) — this is the
already-shipped bridge behavior (pinned by
`test_standard_enum_generates_with_name`); Rust matches it.

**`DROP TYPE` / enum-label cleanup is out of scope.** Emission is additive: an
orphaned enum type left behind by a dropped column/table is harmless and its
removal belongs in a reviewed migration, not auto-migrate.

## The artifact-naming table

All builders live in `crates/ferro-ddl-lowering/src/lib.rs` and are exposed to
Python as `_core._ddl_*`. Guards truncate by **char count** (not bytes) to 63.

| Artifact             | Format                          | Guard (>63 chars)      |
| -------------------- | ------------------------------- | ---------------------- |
| Single-column index  | `idx_<table>_<col>`             | `[:59] + "_idx"` *(FF-B: guard added)* |
| Composite index      | `idx_<table>_<col1>_<col2>...`  | `[:59] + "_idx"`       |
| Single-column unique | `uq_<table>_<col>`              | `[:60] + "_uq"`        |
| Composite unique     | `uq_<table>_<col1>_<col2>...`   | `[:60] + "_uq"`        |
| Check (`db_check`)   | `ck_<table>_<col>`              | `[:60] + "_ck"`        |
| Foreign key          | `fk_<table>_<col>_<to_table>`   | `[:60] + "_fk"` *(FF-B: new)* |

**Single-column uniques are standalone named unique indexes** (`CREATE UNIQUE
INDEX "uq_..."`), not inline column `UNIQUE` and not table-level constraints.
Why: sea-query's create API has no table-level unique, SQLite's ALTER path can
*only* ever add uniques as indexes, and the migrate-updates sentinel requires a
migrated DB to be indistinguishable from a fresh one — so the index shape is
the only one that can be identical across fresh-create, ALTER, and both
backends. The bridge mirrors this by emitting explicit `sa.Index(name, col,
unique=True)` instead of `Column(unique=True)` (which SQLAlchemy materializes
as a differently-shaped `UniqueConstraint`). This resolves
`docs/solutions/issues/sa-vs-rust-unique-constraint-shape.md` (Option A).

**Foreign keys are always named** — both emitters render
`SchemaForeignKey.name`. Two facts discovered while wiring this:

- **Alembic never compares FK names**: autogenerate matches FKs by unnamed
  signature (`alembic/autogenerate/compare/constraints.py:643`, alembic
  1.18.1, `c.unnamed` / `unnamed_no_options`). Adopting named FKs on a DB that
  has anonymous ones therefore never produces an autogen diff, and the Rust
  planner never reconciles FKs on existing columns — so the adoption "drift
  rail" for FK names is documentation-only.
- **sea-query (0.32.7) drops FK names on SQLite** in CREATE TABLE mode (the
  Postgres builder honors `.name(...)`, the SQLite builder never writes
  `CONSTRAINT`). The emitter compensates with a deterministic post-render
  insertion of `CONSTRAINT "<name>" ` before each `FOREIGN KEY` clause — we
  control both the search bytes and the emission order, and golden tests pin
  the output.

## Refusal-rail policy ("never silent ALTERs")

Any storage change on an existing column that could reinterpret or destroy
values is **refused**: auto-migrate emits a warning and skips the ALTER (the
#154 pattern). Single source: `RefusedConversion` +
`refused_conversion_warning` in `ferro-ddl-lowering`, emitted identically by
the IR emitter and the legacy planner (the shadow comparator asserts warning
equality).

| Refusal              | Trigger                                              | Keep recipe                | Convert recipe                          |
| -------------------- | ---------------------------------------------------- | -------------------------- | --------------------------------------- |
| `TimestampTz` (#154) | live `timestamp` ⇄ model `timestamptz`               | `db_type="timestamp"`      | reviewed Alembic + `AT TIME ZONE`       |
| `VarcharToPgEnum`    | live `varchar`/`text`, model native enum (FF-B)      | `db_type="varchar"`        | reviewed Alembic + `USING col::<enum>`  |
| `VarcharToTime`      | live `varchar`/`text`, model `time` (FF-B)           | `db_type="varchar"`        | reviewed Alembic + explicit `USING`     |

Additive changes are not refusals: adopting the `uq_` index shape on an
existing DB emits `CREATE UNIQUE INDEX IF NOT EXISTS` (a redundant second
enforcement next to the old inline artifact until the user drops the old
constraint — see the v0.14.0 migration guide).

## How to recognize a violation

- A name or type spelling appears in `format!()`/f-string form anywhere outside
  `ferro-ddl-lowering` — grep gates:
  `grep -nE '"(idx|uq|ck|fk)_' src/ferro/ir/compiler.py` and
  `grep -n 'logical_type ==' src/ferro/migrations/alembic.py` must stay empty.
- `alembic revision --autogenerate` right after `connect(auto_migrate=True)`
  proposes a type change or an index/constraint drop+create on Postgres.
- The Postgres parity sentinel
  (`tests/test_cross_emitter_parity.py::test_alembic_autogen_against_rust_migrated_db_is_idempotent`)
  fails — never re-filter it; align the emitters.
