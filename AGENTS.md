# AGENTS.md

Hard project invariants for Ferro. These are contracts that **must hold across
every code path**. They are not style preferences. Violating one of these is a
correctness bug.

`.cursorrules` covers project vision, architecture, and TDD workflow. This file
covers the invariants that the architecture rests on.

---

## I-1: Cross-emitter DDL parity

**Every DDL emission path in Ferro must produce byte-identical schema artifacts
for the same model definition.**

Today Ferro can emit DDL through:

- The **Alembic autogenerate bridge** (`src/ferro/migrations/alembic.py`) — used
  when developers run `alembic revision --autogenerate`.
- The **Rust runtime emitter** (`src/schema.rs`) — used when developers call
  `connect(auto_migrate=True)` or generate DDL through the Rust core.
- Any **future emitter** added to the codebase (e.g. a "dump SQL to stdout"
  CLI, a `Ferro.to_sql()` API, an introspection-based diff tool).

For a single model, every emitter must agree on:

1. **Table name** — already handled by `model_name.lower()`.
2. **Column names** — including shadow `*_id` columns from `ForeignKey`.
3. **Column types** — pydantic JSON schema → SQL type mapping must be one
   function (or two functions whose outputs are tested for parity).
4. **Index names** — `idx_<table>_<col>` for single-column indexes,
   `idx_<table>_<col1>_<col2>...` for composite indexes.
5. **Unique constraint names** — `uq_<table>_<col>` for single-column,
   `uq_<table>_<col1>_<col2>...` for composite.
6. **Foreign key constraint names** — when explicitly named.
7. **Primary key constraint names** — when explicitly named.
8. **Check constraint names** — when explicitly named.
9. **Default values** — server-side defaults must serialize identically.
10. **Nullability** — must agree.

### Why this invariant exists

A user can adopt either migration strategy or switch between them. If the
Alembic emitter and the Rust emitter disagree on _any_ schema artifact name,
running `alembic revision --autogenerate` against a database that was bootstrapped
by `connect(auto_migrate=True)` produces phantom diffs — Alembic sees a "missing"
index named `idx_*` and a "spurious" index named `ix_*` and proposes a drop +
create. The migration is technically a no-op but the diff is unreviewable noise
and pollutes the migration history.

Phantom diffs are the canonical symptom that this invariant has been broken.

### How this invariant is enforced

- `src/ferro/migrations/alembic.py` constructs `MetaData` with an explicit
  `naming_convention` that mirrors the Rust emitter (see `_FERRO_NAMING_CONVENTION`).
- `src/schema.rs` hard-codes the same names via `format!("idx_{}_{}",
  table_lower, col_name)` and the helpers in `composite_index_name` /
  `composite_unique_index_name`.
- `tests/test_alembic_autogenerate.py` and `tests/test_schema_constraints.py`
  contain explicit parity tests (`test_index_name_matches_rust_runtime_*`) that
  fail loudly if either side drifts.
- `docs/solutions/patterns/cross-emitter-ddl-parity.md` documents the rule and
  the recipe for adding a new artifact.

### Adding a new emitter

If you add a new emitter (e.g. a "dump schema to JSON" tool):

1. Read the constants in `_FERRO_NAMING_CONVENTION` and the `composite_*_name`
   helpers — these are the source of truth.
2. Add a parity test that compares your emitter's output against the existing
   emitters for at least: single-column index, composite index, single-column
   unique, composite unique, foreign key with shadow column.
3. Update this AGENTS.md entry with the new emitter in the bulleted list above.

### Adding a new artifact

If you add a new schema feature (e.g. partial indexes, exclusion constraints):

1. Pick the canonical name format and document it in this file under the
   numbered list above.
2. Implement it in **both** the Alembic and Rust paths in the same PR.
3. Add a parity test that asserts the names match.
4. Add a regression entry to `CHANGELOG.md`.

---

## I-2: Direct-to-Dict / Zero-copy hydration is non-negotiable

`pydantic-core` `__init__` calls are the single largest source of overhead in
Python ORMs. The Rust core must populate model dicts directly via the bridge
documented in `src/lib.rs` rather than calling `Model(**row)` from Rust.

If you find yourself wanting to call `__init__` from Rust to "make this easier",
stop and read `.cursorrules` §3.B and the design notes under
`docs/solutions/patterns/`.

---

## I-3: No `unwrap()` across the FFI boundary

PyO3 functions must propagate failures via `PyResult` — never panic. Panics
across the FFI boundary unwind into Python as opaque process aborts and ruin
the integration test feedback loop. Use `?`, `map_err`, or explicit
`PyErr::new::<PyTypeError, _>(...)`.

`cargo test` does enforce this for unit tests, but `pytest` is the canonical
gate.

---

## I-4: Tests live with the layer they exercise

- Pure SQL/schema generation logic: `cargo test` (Rust unit tests in
  `src/schema.rs`, etc.).
- Anything that crosses the Python ↔ Rust bridge or exercises Pydantic models:
  `pytest` integration tests under `tests/`.

A feature is not "done" until both sides are green.

---

## I-5: docs/solutions/ is institutional memory

When you discover a non-obvious pattern, gotcha, or architectural decision while
working on Ferro, add it to `docs/solutions/`. Future agents (human and AI) will
search this directory before starting work.

`docs/solutions/patterns/` — design patterns and conventions.
`docs/solutions/issues/` — debugging stories and known footguns.

See `docs/solutions/README.md` for the frontmatter conventions.
