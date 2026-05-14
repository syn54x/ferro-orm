---
title: "Autogenerate support for db_type / db_check"
type: brainstorm
status: draft
date: 2026-05-14
parent: docs/plans/2026-05-13-001-feat-configurable-column-storage-types-plan.md
---

# Autogenerate support for db_type / db_check

> Phase 2 of the configurable-column-storage feature. The DDL-emission half
> shipped in `feat/configurable-column-storage-types` (U1–U5 + trimmed U7).
> This brainstorm captures the deferred migration-detection work so the
> follow-up plan has a clean starting point.

## Problem frame

After Phase 1, a user can declare `Field(db_type="text", db_check=True)` on
a previously native-enum column and `connect(auto_migrate=True)` will create
the table correctly. But when the user switches an **existing** model from
default storage to `db_type="text"` and runs
`alembic revision --autogenerate`, three things happen that should be
automated:

1. The generated `op.alter_column(...)` lacks a `postgresql_using` clause.
   For enum→text the right answer is almost always `USING <col>::text`, but
   autogen can't know that without a hook.
2. After the alter, the previously-used Postgres `CREATE TYPE` is orphaned.
   No `DROP TYPE` is emitted, so the type lingers in the database forever.
3. Narrowing changes (`bigint → int`, `text → varchar(N)` with short N)
   silently risk data loss with no warning in the generated script.

`db_check` add/drop already works via standard Alembic autogen because U3
attaches named `CheckConstraint`s to the SA `MetaData` (R11 is essentially
free) — but this should be confirmed end-to-end with a test before the
follow-up plan declares it shipped.

## Acceptance examples

Carried forward from the original brainstorm
(`docs/brainstorms/2026-05-13-configurable-column-storage-types-requirements.md`):

- **AE5**: A model that previously declared `format: FileFormat` (native
  enum) and now declares `Field(db_type="text")` produces a migration
  containing exactly one
  `op.alter_column("doc", "format", existing_type=sa.Enum(...),
  type_=sa.Text(), postgresql_using="format::text")`
  followed by `op.execute("DROP TYPE fileformat")` after that alter.
- **Multi-table enum sharing**: when two tables reference the same enum and
  only one changes, no `DROP TYPE` is emitted. When both change, exactly
  one `DROP TYPE` is emitted after the last alter.
- **db_check add**:
  `op.create_check_constraint("ck_doc_format", "doc", "format IN (...)")`.
- **db_check remove**:
  `op.drop_constraint("ck_doc_format", "doc", type_="check")`.
- **Narrowing change**: `bigint → int` on Postgres surfaces a warning
  (`warnings.warn` at hook time, or a `-- WARNING:` comment in the
  generated script — TBD during planning).
- **Regression**: a model with no `db_type` change produces an empty
  autogenerate diff.

## Recommended approach

A `process_revision_directives` hook factory in
`src/ferro/migrations/alembic.py`:

```python
from ferro.migrations import process_revision_directives

context.configure(
    target_metadata=...,
    process_revision_directives=process_revision_directives,
    compare_type=True,
)
```

Three passes over `directives[0].upgrade_ops.ops`:

1. **`_inject_using_clauses`** — for `AlterColumnOp` where the type change
   is in a known-safe set (enum→text, etc.), set `kw["postgresql_using"]`.
2. **`_inject_drop_type_for_orphaned_enums`** — collect all
   `AlterColumnOp` whose `existing_type` is a `sa.Enum`. For each enum name,
   check if any column in the target `MetaData` still references it. If not,
   append `ExecuteSQLOp("DROP TYPE <name>")` after the last alter that used
   it.
3. **`_inject_data_loss_warning`** — for narrowing alters, `warnings.warn()`
   or inject a comment-only `ExecuteSQLOp`.

The hook is purely additive and idempotent — running it twice on the same
script must produce the same script.

## Open questions for planning

- **`USING` clause matrix**: which `(from_type, to_type, dialect)` pairs get
  automatic `USING` and which surface as a diff for the user to refine? Start
  with the obvious ones (enum→text gets `::text`; numeric widening doesn't
  need USING) and grow from there.
- **Warning surface for R12**: `warnings.warn(UserWarning, ...)` (visible in
  alembic CLI) vs. injected `-- WARNING:` comment in the generated script
  (visible in code review). Both? Decided when implementing.
- **`Literal[...]` extension for `db_check`**: still deferred from Phase 1.
  The xfail in `tests/test_db_type_validation.py::test_db_check_on_literal_is_accepted`
  is the placeholder.
- **Hook composition**: if the user already has their own
  `process_revision_directives`, document the chaining pattern (call
  Ferro's hook from theirs, or chain ours through theirs).

## References

- Phase 1 plan: `docs/plans/2026-05-13-001-feat-configurable-column-storage-types-plan.md`
- Phase 1 pattern doc:
  `docs/solutions/patterns/configurable-column-storage-types.md` §
  "Autogenerate support (deferred)"
- Original brainstorm:
  `docs/brainstorms/2026-05-13-configurable-column-storage-types-requirements.md`
- Alembic docs: `process_revision_directives`, `MigrationScript`
