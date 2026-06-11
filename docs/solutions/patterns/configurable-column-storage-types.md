---
title: Configurable column storage types (db_type / db_check)
type: pattern
tags: [convention, schema, alembic, sea-query, enum, validation, migrations]
related_files:
  - src/ferro/fields.py
  - src/ferro/base.py
  - src/ferro/metaclass.py
  - src/ferro/schema_metadata.py
  - src/ferro/_annotation_utils.py
  - src/ferro/migrations/alembic.py
  - src/schema.rs
  - src/operations.rs
  - tests/test_schema_db_type_metadata.py
  - tests/test_db_type_validation.py
  - tests/test_alembic_db_type.py
  - tests/test_db_type_cross_emitter_parity.py
related_issues: []
related_prs: []
captured: 2026-05-14
---

## Problem

Ferro's default behavior maps every Python `Enum`-typed field to a native
PostgreSQL enum type. Native enums make schema evolution expensive
(`ALTER TYPE ... ADD VALUE`, multi-step renames, `DROP TYPE` ordering). The
same shape of problem exists for other Python types: a user may want an
`int` field stored as `BIGINT` rather than `INTEGER`, a `UUID` field stored
as portable `TEXT` rather than the native `UUID` type, or a `datetime` field
stored as `TIMESTAMP WITH TIME ZONE` rather than plain `TIMESTAMP`.

Pre-feature, users had no way to express "validate this in Pydantic but
store it as something simpler" without writing custom Alembic migrations and
giving up Ferro's `connect(auto_migrate=True)` path.

## Takeaway

`Field()` accepts two opt-in kwargs that control DDL storage independently
of the Python type:

```python
class Doc(Model):
    id: int | None = Field(default=None, primary_key=True)
    format: FileFormat = Field(db_type="text", db_check=True)
    value: int = Field(db_type="bigint")
    external_id: UUID = Field(db_type="text")
    occurred_at: datetime = Field(db_type="timestamptz")
```

- `db_type` — pick a canonical SQL storage type. Validation at class-
  definition time rejects incoherent combinations (e.g. `db_type="int"` on a
  `StrEnum` field) with a `TypeError`.
- `db_check` — add a DB-side `CHECK (col IN (...))` constraint named
  `ck_<table>_<col>`. Only valid for closed-domain Python types (`Enum`
  subclasses) and only when `db_type` is also set (the default native-enum
  path already enforces values, so `db_check=True` without `db_type` is a
  `TypeError`).

Both kwargs flow through the same `FERRO_FIELD_EXTRA_KEY` plumbing as
`primary_key` / `unique` / `index` and land on `properties[col]` in
`__ferro_schema__`. Both emitters read them from the same JSON.

## Canonical vocabulary

Every accepted `db_type` token, owned by
`src/ferro/_annotation_utils.py::CANONICAL_DB_TYPES`:

| Token         | SA type (Python)               | sea-query (Rust)                       |
| ------------- | ------------------------------ | -------------------------------------- |
| `text`        | `sa.Text`                      | `text()`                               |
| `varchar(N)`  | `sa.String(N)`                 | `string_len(N)`                        |
| `smallint`    | `sa.SmallInteger`              | `small_integer()`                      |
| `int`         | `sa.Integer`                   | `integer()`                            |
| `bigint`      | `sa.BigInteger`                | `big_integer()`                        |
| `uuid`        | `sa.Uuid` / `sa.String(36)`    | `uuid()` (PG) / `char_len(32)` (SQLite)|
| `timestamp`   | `sa.DateTime(timezone=False)`  | `timestamp()` (PG) / `date_time()` (SQLite) |
| `timestamptz` | `sa.DateTime(timezone=True)`   | `timestamp_with_time_zone()` (PG) / `date_time()` (SQLite) |
| `date`        | `sa.Date`                      | `date()`                               |
| `time`        | `sa.Time`                      | `time()`                               |

Per-dialect rendering on SQLite is chosen to byte-match SQLAlchemy's own
compilation. SQLite emits the typed keyword (`BIGINT`, `SMALLINT`,
`DATETIME`, `CHAR(32)`) and lets SQLite type affinity normalize at runtime;
the parity test
(`tests/test_db_type_cross_emitter_parity.py`) pins this token-for-token.

## Compatibility matrix

Class-definition validation rejects incoherent `(annotation, db_type)`
pairs:

| Token family                    | Accepted annotations                            |
| ------------------------------- | ----------------------------------------------- |
| `text` / `varchar(N)`           | `str`, `StrEnum` / string-valued `Enum`, `UUID` |
| `smallint` / `int` / `bigint`   | `int`, `IntEnum` / int-valued `Enum`            |
| `uuid`                          | `UUID` only                                     |
| `timestamp` / `timestamptz`     | `datetime.datetime` only                        |
| `date`                          | `datetime.date` only                            |
| `time`                          | `datetime.time` only                            |

`db_check=True` requires the annotation to be an `enum.Enum` subclass (Phase
1; `typing.Literal[...]` support is deferred — there is an `xfail` test
guarding the eventual extension point in `tests/test_db_type_validation.py`).

## Architecture: duplicated dispatch, paired parity test

Two emitters, two dispatch tables, one parity test:

- **Python:** `_db_type_to_sa_type(token)` in
  `src/ferro/migrations/alembic.py` returns an SA type.
- **Rust:** `db_type_token_to_canonical(token, backend)` in
  `src/schema.rs` resolves a `CanonicalType`, which `apply_canonical_type`
  applies to a `ColumnDef`.
- **Parity test:**
  `tests/test_db_type_cross_emitter_parity.py::test_column_type_parity_across_emitters`
  walks every `(token × dialect)` pair, renders both emitters via
  `_render_create_table_sql_for_test` (Rust) and `CreateTable(...).compile(dialect)`
  (Python), and asserts the rendered SQL contains the same keyword.

This mirrors the `composite_index_name` precedent: duplicated constants
enforced by a paired test, not a shared data file. No build-time dependency
between Python and Rust is introduced.

The `db_check` constraint name is similarly duplicated:
`_ck_constraint_name` (Python) and `db_check_constraint_name` (Rust) both
produce `ck_<table>_<col>` with the same 63-character Postgres-identifier
truncation guard.

## Recipe: adding a new canonical token

1. Add the token to `CANONICAL_DB_TYPES` in
   `src/ferro/_annotation_utils.py`, plus its compatibility predicate.
2. Add an arm in `_db_type_to_sa_type` (Python, `migrations/alembic.py`)
   returning the appropriate `sa.types.TypeEngine`.
3. Add an arm in `db_type_token_to_canonical` (Rust, `src/schema.rs`)
   returning the matching `CanonicalType` variant (add one if needed, with
   its `ColumnDef::*` mapping in `apply_canonical_type`), with per-backend
   resolution chosen to byte-match the Python side's
   `CreateTable.compile(dialect)` output.
4. Extend `_TOKEN_CASES` in `tests/test_db_type_cross_emitter_parity.py`
   with the new token and expected keywords for both `postgres` and `sqlite`.
5. Add a positive validation test and at least one negative
   (incoherent-annotation) test in `tests/test_db_type_validation.py`.
6. Update the canonical-vocabulary tables in this file and in
   `AGENTS.md § I-1`.
7. Mention the new token in `CHANGELOG.md` under the next release.

## SQLite-specific notes

- `db_check=True` is currently elided on SQLite at the Rust emitter level
  because SQLite cannot `ALTER TABLE ... ADD CONSTRAINT`. The Alembic side
  still attaches the `CheckConstraint` to the SA Table (SA can render an
  inline CHECK for SQLite at `CREATE TABLE` time). This asymmetry is pinned
  by `test_db_check_elided_on_sqlite_in_both_emitters`.
- SQLite type affinity makes `BIGINT` / `SMALLINT` keywords cosmetic — the
  runtime storage is the same. Both emitters emit the typed keyword anyway
  so that user-facing introspection matches the model declaration.

## Autogenerate support (deferred)

Phase 1 ships the DDL-emission half of the feature. Alembic autogenerate
already produces the right `op.alter_column(...)` ops when `db_type` changes
between revisions, because U3 makes the SA `MetaData` reflect the new type.
What it does **not** yet do automatically:

- Append `postgresql_using="<col>::<new_type>"` to `alter_column` ops where
  the type change needs a cast clause.
- Emit a deferred `DROP TYPE <enum>` after the last `alter_column` that
  drops the final reference to a native Postgres enum type.
- Emit a comment / warning for narrowing changes (`bigint → int`,
  `text → varchar(N)` with short N).

These are tracked for a follow-up plan and require a
`process_revision_directives` hook on the user's `env.py`. Workarounds for
the current release:

- The `op.alter_column` is correct as-is; the user can hand-edit the
  `USING` clause if Alembic's default cast fails on production data.
- The orphaned `CREATE TYPE` is cosmetic; the user can drop it manually
  with `op.execute("DROP TYPE <name>")` in the generated script.

## How to recognize related issues

- `TypeError` at import time naming a field and a `db_type` token: read the
  message — it names both the rule violated and the canonical vocabulary.
- Parity-test failures in `tests/test_db_type_cross_emitter_parity.py`: one
  emitter's dispatch has drifted. Re-align both halves of the duplicated
  dispatch and re-run; never silence the test.
- `op.alter_column` autogen leaving a stale `CREATE TYPE` around: expected
  pre-Phase-2. See "Autogenerate support" above.
