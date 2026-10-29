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

Take one edit, `bio: str | None = None` added to `Author`. Each door below
runs the same statement for it, `ALTER TABLE "author" ADD COLUMN "bio"
varchar`, because each asks the same planner and renders through the same
functions. Today Ferro emits DDL through:

- The **reconciliation pass** (`src/schema.rs`, `src/migrate.rs` over
  `crates/ferro-migrate`): `connect(auto_migrate=True)` and its
  `migrate_updates` / `migrate_destructive` rungs. It reads the live database
  (`src/live_ir.rs`, `_core._live_schema_ir`), plans with the one planner
  (`ferro_migrate::plan_from_ir`) and runs its `Plan::render`; its
  create pass is `ferro_migrate::render_create_table`. What it executed is its
  `PassReport` (`ferro.migrate()` / `ferro.create_tables()` return it, built
  from what the DDL executor ran, ADR-0049), pinned against the planner by
  pin **(g)**.
- The **migrations door** (`src/ferro/migrations/` +
  `crates/ferro-migrate/src/generate/`): `ferro migrate new`. It plans between
  two schema snapshots with the same planner and renders through the same
  `Plan::render`, once per dialect, into step files. It decides which step an
  op lands in and which headers a file carries, never a statement. Its
  online shapes are the pass's renderings in another mode: `NOT VALID` then a
  validate step (`ConstraintMode`, ADR-0043), `CONCURRENTLY` (`IndexMode`,
  ADR-0044), the `_ferro_notnull_<table>_<col>` staged `NOT NULL`
  (`staged_not_null_name`, never `ck_*`, #534), the SQLite table rebuild
  (ADR-0046) and the Postgres enum type swap for a removed label (#536).
- The **Alembic autogenerate bridge** (`src/ferro/migrations/alembic.py` +
  `src/ferro/migrations/translate.py`): `alembic revision --autogenerate`. It
  translates the one planner's ops (ADR-0041, item 11).
- Any **future emitter** added to the codebase (e.g. `ferro schema dump`, a
  `Ferro.to_sql()` API, an introspection-based diff tool).

For a single model, every emitter must agree on:

1. **Table name** — already handled by `model_name.lower()`.
2. **Column names** — including shadow `*_id` columns from `ForeignKey`.
3. **Column types** — decided by ONE function:
   `ferro_ddl_lowering::resolve_column_storage` (explicit `db_type` token →
   native-enum resolution → the `canonical_from_parts` cascade). The Alembic
   bridge consumes it mechanically over FFI (`_core._resolve_storage_type`)
   and `_db_type_to_sa_type` is only the SA *rendering* of the shared token
   vocabulary — never a second decision table. Pinned exhaustively by
   `tests/test_db_type_cross_emitter_parity.py` (every token and every
   derived annotation × both dialects, and every token through a generated
   `ADD COLUMN`). See
   `docs/solutions/patterns/derived-type-and-naming-decision-table.md`.
4. **Index names** — `idx_<table>_<col>` for single-column indexes,
   `idx_<table>_<col1>_<col2>...` for composite indexes.
5. **Unique constraint names** — `uq_<table>_<col>` for single-column,
   `uq_<table>_<col1>_<col2>...` for composite.
6. **Foreign key constraint names** — `fk_<table>_<col>_<to_table>`, always
   emitted (every emitter renders `SchemaForeignKey.name`; single-sourced in
   `ferro_ddl_lowering::fk_name`). See
   `docs/solutions/patterns/derived-type-and-naming-decision-table.md`.
7. **Primary key constraint names** — when explicitly named.
8. **Check constraint names** — `ck_<table>_<col>` for the single-column
   `db_check=True` constraint; `ck_<table>_<suffix>` for table checks declared
   in `__ferro_checks__`. Column checks use `_ddl_check_constraint_name`
   (Python) and `db_check_constraint_name` (Rust); table checks use
   `_ddl_table_check_constraint_name` (Python) and
   `table_check_constraint_name` (Rust).
   A generated column's SQLite range guard is `_ferro_range_<col>`
   (`sqlite_guard_name`), part of the column and never a `ck_*` check.
9. **Default values** — server-side defaults must serialize identically.
   One exception (ADR-0034): on SQLite the reconciliation pass adds a
   required column with a literal default as `NOT NULL DEFAULT <literal>`
   and keeps that default, since only a rebuild could drop it; a migration
   rebuilds the table without it (`KEPT_DEFAULTS` in
   `tests/test_cross_emitter_parity.py`).
10. **Nullability** — must agree.
11. **Every change to an existing database: the Alembic bridge translates the
    one planner's ops.** What changes and the statement that changes it are
    decided once, by the one planner (`ferro_migrate::plan_from_ir`) and its
    renderer (`ferro_migrate::Plan::render`), over function families in
    `ferro_ddl_lowering`. Every op leaves the planner with its
    `ferro_migrate::OpVerdict`, decided once (ADR-0050): whether the dialect
    runs it natively, rebuilds the table, refuses it (naming the fix) or
    cannot reverse it, and whether it demands values from existing rows,
    drops data or can fail on them, whether it is the drop or add of a
    generated column the plan replaces (`replaces`) and whether it carries
    an unchanged policy or check around another change (`carried`). Every door reads that one verdict and
    re-decides none of it. The reconciliation pass runs them directly; the
    migrations door writes them into step files; the bridge's one comparator
    (`dispatch_for("schema")` in `src/ferro/migrations/alembic.py`) reads the
    live database through `_core._live_schema_ir` and asks the core for the
    whole revision in one call, `_core._plan_revision`
    (`ferro_migrate::plan_revision`): the upgrade planned live → models with
    destructive changes on, its `downgrade()` the one down every door uses,
    `ferro_migrate::plan_down`, run from the models back to the live
    database and scoped to the artifacts the upgrade touched (ADR-0050; a
    check or policy put back with the body the catalog printed through
    `render_check_restore`), both rendered by the one renderer, with every
    refusal, marker, irreversible reason, Alembic-twin choice, autocommit
    and foreign-key rider decided there. `translate.py` writes that answer
    and decides nothing: Alembic's own op where the core says it has one,
    `op.execute(sa.DDL(...))` of the pass's statement, byte for byte, where
    it does not. The per-family comparators, their slot registrations and
    the `_plan_check_*` FFI are gone (#533).
    The families, each one decision consumed by every door:
    - **Enum labels** — `missing_enum_labels` / `extra_enum_labels` +
      `render_pg_enum_add_value` (ADR-0011: append-only, update-gated; an
      extra live label is a warning on a live database, and between two
      declared snapshots the migrations door's removal, #536). A label
      rename hint is `render_pg_enum_rename_value` (`render_label_update`
      on SQLite, in a migration only; the pass warns there, ADR-0047); an
      enum class rename `render_pg_enum_rename_type`.
    - **Checks** — additions (`missing_check_names` /
      `render_check_addition`), rebuilds (`drifted_check_names` /
      `render_check_rebuild`) and leftovers (`extra_check_names` /
      `extra_check_names_warning` / `render_check_drop`) (ADR-0013..0015).
      On Postgres a body is compared as a **printed pair** (ADR-0060): the
      declared rendering and the catalog's reprint pass through one
      read-only `EXPLAIN (VERBOSE, FORMAT JSON)` per compared table
      (`printed_pair_checks`, `PrintedPair`), and equal prints are no
      drift. On SQLite, which stores the body as written, the normalizer
      `normalize_check_definition` compares the canonical rendering with
      the stored text. Row-policy drift keeps its own normalizer
      (`normalize_row_policy_expr`).
    - **Row security** — the policy name, shorthand cast, rendered
      expression and `ENABLE` / `FORCE` / `CREATE POLICY` statements
      (`row_policy_name`, `is_ferro_row_policy_name`,
      `row_policy_shorthand_cast`, `render_row_policy_setting_expr`,
      `render_create_row_policy`, `row_security_statements`, over
      `ROW_POLICY_COMMANDS`), and the reconciliation of a live table
      (`plan_row_security_reconcile`, with `is_default_row_policy_roles`,
      `ferro_manages_row_security`, `normalize_row_policy_expr`,
      `row_policy_command_from_catalog_code` and its warning texts), whose
      flags are decided by `missing_row_security_flags` (the flags to turn
      on) and `excess_row_security_flags` (the flags to turn off,
      `migrate_destructive` only), each a `Vec<RowSecurityFlag>`. The
      Python declaration surface (`src/ferro/rowsecurity.py`) consumes the
      name, the cast and the command table over FFI
      (`_core._ddl_row_policy_name`, `_core._rls_shorthand_cast`,
      `_core._rls_command_matrix`). A foreign policy and an unverifiable raw
      body never become an op on any door (ADR-0019). Postgres-only; SQLite
      gets one warning per table and no DDL (ADR-0014).
    - **Enum types** — a type the change introduces is always the pass's
      guarded `CREATE TYPE` (`render_pg_enum_create_type`) ahead of every
      table op, and a type it retires is `render_pg_enum_drop_type` after
      them; the bridge renders every enum column `create_type=False`
      (`ferro.migrations.render_item`). This supersedes in part the
      inline-vs-statement provenance of ADR-0020..0022 (ADR-0041).
      Postgres-only (SQLite enums store as text).
    - **Generated expressions** — take
      `direction: int = Field(generated=lambda ledger: fn.sign(ledger.amount_minor))`.
      Every door writes the column as
      `GENERATED ALWAYS AS (CAST(sign(CAST("amount_minor" AS bigint)) AS bigint)) STORED`
      on Postgres and `GENERATED ALWAYS AS (sign("amount_minor")) STORED
      CONSTRAINT "_ferro_range_direction" CHECK (typeof("direction") IN
      ('integer', 'null') AND "direction" BETWEEN -2147483648 AND
      2147483647)` on SQLite, never with a `DEFAULT`. The body is lowered
      once (`render_value_expr` over the closed function table
      `value_function`, which `_core._value_function_table` hands `ferro.fn`;
      the clause is `render_generated_clause`) at the type
      `type_value_expr` gives it, the type SQLite computes it at: on
      Postgres an integer value computes at 64 bits and a float one as a
      `double precision`, each operand or call Postgres would type
      otherwise (a `smallint` or `integer` column, `sign`, `length`, a
      date part, a literal) cast to that type. Its ops and their verdicts: added with the
      column (`AddColumn`, native on Postgres, a rebuild on SQLite, no
      backfill), a changed expression (`AlterGeneratedExpression`,
      `render_set_generated_expression`, can fail on rows), generated to
      plain (`DropGeneratedExpression`, `render_drop_generated_expression`,
      values kept, `migrate_updates`) and an input rename (the rename
      alone). Three changes drop the column and add it back, each op's
      verdict naming why in `OpVerdict.replaces`: plain to generated
      (`Replace::MadeGenerated`, destructive; without `migrate_destructive`
      the pass reports `PlainColumnKept`), an input's type change
      (`Replace::InputType`) and its own type change (`Replace::OwnType`),
      both of which Postgres refuses to alter under an expression. A row
      policy or table check that reads the column is carried around the
      pair. A carried artifact is one taken down and put back unchanged
      around another change, its ops marked `OpVerdict.carried`, which no
      door reports as a difference; a changed or uncomparable artifact is
      a difference drift reports. A row policy the plan
      cannot carry (a raw body it may not rewrite, ADR-0019) refuses the
      drop with `Refusal::PolicyReadsReplacedColumn`, naming the
      `DROP POLICY` recipe. Drift is a printed pair on Postgres
      (`printed_pair_column`; a live `VIRTUAL` column or a plain one where
      the model declares generated differs) and Ferro's rendering against
      the stored text, whitespace folded, on SQLite. The bridge writes an
      added column as `sa.Computed(<expr>, persisted=True)` and the other
      ops as `op.execute` of the pass's statement; `get_metadata()` gives a
      SQLite column its guard as a column `CheckConstraint`
      (`_core._render_sqlite_generated_guard`), so `create_all` builds the
      pass's table.
      The SQLite guard `_ferro_range_<col>` (`sqlite_generated_guard`)
      is written inside the column definition and compared with it: never
      a `ck_*` check and never in `__ferro_checks__`. Postgres raises where
      a value does not fit; SQLite would store a `real` or `inf`. So the
      guard holds a `smallint` or `integer` column to its Postgres range
      and a `bigint` one to staying an integer, and every operation under
      the column that can fail on a value is checked on SQLite the way
      Postgres evaluates it (a `coalesce` fallback only when the earlier
      arguments are null): an integer result must still be an integer, a
      float one finite unless an operand already was infinite. A
      `double precision` column gets no storage term, since
      Postgres stores the `inf` an input carried. `total: int = a + b`
      carries only the storage term; Postgres needs no guard, since it
      computes at 64 bits and its column type holds the range.
    - **Value parity** — a value expression (a generated column's body, or
      either side of a check's comparison that calls `fn`) is admitted only
      when SQLite and Postgres store the same value for it on every row,
      and fail on the same rows, decided once when the model is defined
      (ADR-0059). Two functions decide it, and every door reads them:
      `ferro_ddl_lowering::type_value_expr` types each node (family and
      nullability, from the columns, `literal_type` and the signatures in
      `VALUE_FUNCTIONS`) and refuses what would store differently, read by
      the class definition over FFI (`_core._value_expr_type`), the
      Postgres renderer and the SQLite guard; `value_step_can_fail` names
      the one operation that can fail on a value (an integer or float `+`,
      `-` or `*`, a `/ -1`, an integer unary `-`, `abs` of an integer),
      read by the SQLite guard, the check body, the refusal of such an
      operation under an `OR` (`_core._value_expr_failing_step`) and the
      error mapping that turns the overflow into a `DataError` naming the
      column or check (`table_check_computes`). The refusals: the
      expression's type family matches the column's; `least` / `greatest`
      take numbers or dates and no nullable operand; `year` / `month` /
      `day` take a `date`; `/` only by a non-zero literal; no float `*` or
      `/` and no Decimal arithmetic; no integer literal past 64 bits; no
      bounded text storage, which Postgres pads or trims and SQLite does
      not; no
      other generated column, retired column, relation or closed-over
      variable. A check's SQLite body guards each comparison with a failing
      operation (`render_check_predicate`): `lambda box: box.a * box.b > 0`
      is
      `CASE WHEN NOT (typeof(("a" * "b")) IN ('integer', 'null')) THEN
      abs(-9223372036854775807 - 1) ELSE (("a" * "b") > 0) END`, whose
      `abs` raises SQLite's own `integer overflow` where Postgres raises, so
      `NOT` cannot invert it and a conjunct both databases skip computes
      nothing; the Postgres body carries no terms. A function joins `fn`
      only with its rendering per dialect and a row in
      `tests/test_value_parity.py`, which runs it on both dialects; a
      divergence is refused, never documented.

    Every artifact above reaches every door through the one planner,
    `ferro_migrate::plan_from_ir` (`_core._plan_from_ir` over FFI): its
    per-family deciders are private to it and pinned through it, never as a
    second entry point; `_core._plan_enum_type_provenance` remains as a
    pass-side parity pin. The item is carried by pins **(e)** and **(f)**
    below.

### The nine pins

`tests/test_cross_emitter_parity.py` pins the migrations door, the bridge and
the pass itself against the one planner over every casebook change
(`tests/_casebook.py`, cases A–G, built from the generator tests' own models)
on both dialects:

- **(a)** the statements of the generated DDL steps, headers stripped and the
  online shapes compared by their plain twins (`normalize_online_shape`: a
  `NOT VALID` add is the plain add, `CONCURRENTLY` is gone, a validate or
  staging statement is nothing, a rebuild is the ops it carries), equal the
  pass's plan for the same before/after modelset
  (`_core._plan_from_ir(..., render=True)`, two declared snapshots);
- **(b)** a rebuild's `CREATE TABLE "_ferro_new_<t>"` is byte for byte the
  create pass's rendering of the shape its step leaves (the relaxed shape for
  an expand, the target for a contract), apart from the table name;
- **(c)** a concurrent index statement equals the pass's by exactly one token
  (`IF NOT EXISTS` against `CONCURRENTLY`);
- **(d)** the validate, label-addition and type-creation statements are the
  pass's, through the one renderer;
- **(e)** a database taken through the migration chain shows no drift, has
  the same live schema (facts included) as one `connect(auto_migrate=True)`
  built from the same models, the same server default on every column
  (no backfill `DEFAULT` left behind, and a renamed table's serial sequence
  under the name a fresh table gives it) and the same expression on every
  generated column (`generation_expressions`), and Alembic autogenerate against
  the auto-migrated one is empty (against the migrated one it refuses: a
  tracked database is `ferro migrate new`'s);
- **(f)** the bridge's revision runs the pass's DDL for every planner op:
  every statement it runs as written is the pass's, and it leaves the same
  live schema the pass's statements do;
- **(g)** the pass executes what the planner renders: on a database at the
  case's before side, `ferro.migrate(destructive=True)`'s `PassReport`
  `schema` statements equal `_core._plan_from_ir(..., render=True)` for the
  same live read, grouped by table (or enum type) and in order within each,
  the create pass standing in for each `AddTable`; its warnings equal the
  plan's reports by kind and subject, never by sentence;
- **(h)** a squash is `new` across its ends: on a copy of the migrations
  directory, `ferro migrate squash NAME --from 0001` writes the one `0001`
  that `ferro migrate new NAME` writes from the after models alone, file for
  file and byte for byte, bar `ir.json`, whose payload is the same once the
  squash's witness and the composed rename hints are removed;
- **(i)** an inherited database is the squashed one: a database taken through
  `0001` and `0002` and then inherited by `ferro migrate up` against the squashed
  directory (it runs no statement) has the same live schema as one that ran
  the squashed `0001` from empty, reading every fact pin (e) reads (columns,
  types, nullability, indexes, constraints, policies, the server default
  of every column, `column_defaults`, and the expression of every generated
  column, `generation_expressions`), the same `_ferro_migrations` rows bar
  origin, `ferro_version` and times, no drift on either, and
  `ferro migrate down --all` leaves both with no table of the models and no
  record.

A pin that says "the same live schema" must list the catalog facts it reads,
because a fact it does not read is a gap it cannot see. Pin (e) compared
columns, types, nullability, indexes, constraints and policies but not server
defaults, and that hid three real parity gaps for the whole epic: an invented
enum `DEFAULT`, SQLite's leftover backfill `DEFAULT`, and a renamed table's old
serial sequence. They surfaced only once the pin started reading
`column_default` (#605, #608).

A pin that fails against merged behaviour is listed in `FINDINGS` there,
`xfail(strict=True)` with its reason, until the fix lands and the strict
xfail removes it.

### Why this invariant exists

A user can adopt any migration strategy or switch between them. If two doors
disagree on _any_ schema artifact, the second one sees phantom diffs: running
`alembic revision --autogenerate` or `ferro migrate drift` against a database
that `connect(auto_migrate=True)` bootstrapped proposes a drop + create of an
index named `ix_*` for one named `idx_*`. The migration is technically a
no-op, but the diff is unreviewable noise and pollutes the migration history.

Phantom diffs are the canonical symptom that this invariant has been broken.

### How this invariant is enforced

- One planner, one renderer: every door plans with
  `ferro_migrate::plan_from_ir` and renders with `Plan::render`; every
  decision lives in one `ferro_ddl_lowering` function, consumed over FFI by
  the Python side.
- `src/ferro/migrations/alembic.py` constructs `MetaData` with an explicit
  `naming_convention` that mirrors the Rust emitter (see `_FERRO_NAMING_CONVENTION`).
- `src/schema.rs` hard-codes the same names via `format!("idx_{}_{}",
  table_lower, col_name)` and the helpers in `composite_index_name` /
  `composite_unique_index_name`.
- `tests/test_cross_emitter_parity.py` holds the bridge sentinel (autogenerate
  against an auto-migrated database is empty) and the nine pins; `tests/test_db_type_cross_emitter_parity.py` pins every type token;
  `tests/test_alembic_autogenerate.py` and `tests/test_schema_constraints.py`
  pin the names (`test_index_name_matches_rust_runtime_*`).
- `docs/solutions/patterns/cross-emitter-ddl-parity.md` documents the rule and
  the recipes below.
- On the query side, the rendered-statement parity pin in
  `tests/test_rendered_statement.py` is the counterpart of the cross-emitter
  pins: for every `Query` terminal (`all`, `first`, `count`, `exists`,
  `update` in both forms, `delete`, through `Relation` and `ProjectedQuery`
  too) on both dialects, with a secret column, `await query.<terminal>.sql()`
  equals the `ferro.sql` record the executed call writes in `sql`, `params`
  (the `REDACTED` sentinel included) and `dialect`. Both doors build the
  statement through one `build_<terminal>` function in `src/operations.rs`,
  which the executing entry sends and its `render_<terminal>` twin returns.

### Adding a new emitter

If you add a new emitter (e.g. `ferro schema dump`):

1. Plan with `ferro_migrate::plan_from_ir` and render with `Plan::render`;
   read the constants in `_FERRO_NAMING_CONVENTION` and the `composite_*_name`
   helpers — these are the source of truth. Never a second renderer.
2. Add a pin to `tests/test_cross_emitter_parity.py` that compares your
   emitter's output with `_core._plan_from_ir(..., render=True)` for every
   casebook case on both dialects, the way pin (a) does for the migrations
   door; at least single-column index, composite index, single-column
   unique, composite unique and foreign key with shadow column must be
   among them.
3. Update this AGENTS.md entry with the new emitter in the bulleted list above.

### Adding a new artifact

If you add a new schema feature (e.g. partial indexes, exclusion constraints):

1. Pick the canonical name format and document it in this file under the
   numbered list above.
2. Decide it in one `ferro_ddl_lowering` function, plan it as a
   `MigrationOp` and render it in `Plan::render`, so every door gets it in the
   same PR; give the bridge's `translate.py` its op (Alembic's own, or the
   pass's statement).
3. Add a casebook case (`tests/_casebook.py`) so pins (a)–(i) cover it, and a
   parity test that asserts the names match.
4. Do not edit `CHANGELOG.md` manually — release tooling records entries at
   release time (see I-10).

---

## I-2: Direct-to-Dict / Zero-copy hydration is non-negotiable

`pydantic-core` `__init__` calls are the single largest source of overhead in
Python ORMs. The Rust core must populate model dicts directly via the bridge
documented in `src/lib.rs` rather than calling `Model(**row)` from Rust.

Hydrated instances must still be **observationally equivalent** to instances
constructed through `BaseModel.__init__` for Pydantic’s own slot attributes:
anything in `BaseModel.__slots__` that `__init__` assigns (notably
`__pydantic_extra__` and `__pydantic_private__`, in addition to
`__pydantic_fields_set__`) must be initialized on the Rust hydration path as
well. Leaving a slot unset raises `AttributeError` on access (unlike a normal
instance attribute defaulting to `None`).

The same holds for field values: a `SecretStr` / `SecretBytes` field hydrates
as that type, never as the plain `str` / `bytes` the column stores. A
hydrated instance that shows a secret's value where a constructed one shows
`**********` is an I-2 miss (#760; review I-38 in #786).

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

---

## I-6: No stop-gap solutions

Every feature, bug fix, and improvement must be designed as the best,
well-thought-out solution for the project with the library's future in
mind — as if time and money were no object. No stop-gaps, hacks,
quick-fixes, or otherwise lesser solves.

What this means in practice:

- **Prefer first-class, reusable primitives over local patches.** If a fix
  only works for the immediate symptom while leaving the underlying
  capability gap in place, build the capability instead. (Precedent:
  `EngineHandle::refresh_pool()` was built as an engine-level schema-epoch
  primitive rather than a migration-local statement-cache flush.)
- **Fail loudly over degrading silently.** "Skip with a warning and
  continue", "best effort", and "documented residual risk" are not
  acceptable resolutions for correctness gaps. Either the operation
  succeeds completely or it aborts with a clear, actionable error.
- **Treat certain phrases as redesign triggers.** If a plan, comment, or PR
  description contains "best-effort", "partial mitigation", "documented
  residual risk", "good enough for now", "temporary workaround", or
  "fallback if X turns out to be hard" — that part of the design is not
  finished. Redesign it before presenting or implementing it.
- **Scoped-down is fine; hollowed-out is not.** Deliberately excluding
  something from scope (with the boundary stated and a real path for the
  excluded case, e.g. "renames are Alembic territory") is good design.
  Shipping a half-working version of something that is *in* scope is not.
- **Every refusal names its fix.** A refusal tells the reader what to do
  next: the command, the line or the recipe. This binds every refusal text
  the migration doors print: the primary-key recipe, the SQLite rebuild
  refusals (an undeclared object, a type SQLite cannot check), the
  tracked-database refusal of `connect(auto_migrate=True)` and of Alembic
  autogenerate, and the edited-file refusals (`ferro migrate rerecord`,
  `--continue` / `--restart`).

This rule binds human contributors and AI agents equally, and overrides any
agent default that biases toward minimal or expedient changes.

---

## I-7: Docs examples show both field-declaration styles

Ferro supports two equivalent ways to declare model fields: assignment
(`name: str = Field(unique=True)`) and `Annotated` metadata
(`name: Annotated[str, Field(unique=True)]`). Every documentation example
that declares model fields with `Field()`/`FerroField()` options must show
**both** styles, side by side, as content tabs:

    === "Assignment"

        ```python
        --8<-- "docs/examples/<example>.py:models"
        ```

    === "Annotated"

        ```python
        --8<-- "docs/examples/<example>_annotated.py:models"
        ```

Rules:

- Both tabs must be backed by real, runnable code. Snippet-embedded model
  definitions get a runnable `<name>_annotated.py` companion in
  `docs/examples/` (exercised by `tests/test_docs_examples.py`); inline
  blocks are written in both styles and compile-checked by the same test.
- Constructs with only one valid form appear identically in both tabs and
  are not tabbed on their own: forward FKs are always
  `Annotated[Target, ForeignKey(...)]`, and `BackRef()` / `ManyToMany()`
  are always assignments.
- Code blocks that do not declare fields (queries, mutations, transactions,
  usage snippets) are not affected by this rule.

This keeps users from ever wondering whether something is possible in their
preferred declaration style.

---

## I-8: Queries are lambda predicates named after the model

A query filters with a lambda whose parameter is named after the model, in
lowercase singular:

```python
adults = await User.where(lambda user: user.age >= 18).all()
recent = await Post.order_by(lambda post: post.published_at, "desc").all()
```

Lambdas are the only predicate form. `where()` raises `TypeError` on anything
that is not a callable; the `col()` and operator-style predicates were removed
in 0.14.

Rules:

- **Every query example** in docs pages, docstring `Examples:` sections and
  `docs/examples/` scripts passes lambdas to query methods (`where`,
  `select`, `order_by`, `include`, `exists`, `count`, `aggregate`,
  `group_by`, `after`, `before` and every other method that takes a
  callable).
- **Name the parameter after the model in lowercase singular**: `user` for
  `User`, `post` for `Post`, `line_item` for `LineItem`, so the predicate
  reads as the sentence it means. A traversal keeps the root model's name
  (`lambda transaction: transaction.account.label`).
- **`order_by`** takes a column-name string (`order_by("created_at",
  "desc")`) or a lambda selector (`order_by(lambda post: post.created_at,
  "desc")`); anything else raises `TypeError`. The lambda is the documented
  form, and the only one that orders by a related column
  (`order_by(lambda transaction: transaction.account.label)`).
- **The lint** `uv run --no-sync python -m tests._docs_checks PATH...` lists
  every lambda passed to a query method whose parameter is one character,
  as `path:line: message`, and exits 1. It reads Python files, Python fences
  in Markdown and docstrings. `tests/test_docs_lint.py` runs it over
  `docs/pages`, `docs/examples` and `src/ferro`, so a violation fails the
  suite; run it yourself over every docs page, example and docstring you
  touch.

`docs/pages/guide/queries.md` and `docs/pages/concepts/query-typing.md`
explain how lambda predicates are evaluated and typed; every other page uses
them without restating that.

---

## I-9: PRs must close scoped issues explicitly

Issue closure is part of feature completion, not optional cleanup.

Rules:

- Every PR that completes scoped work **must** include GitHub auto-close
  keywords in the PR body for each completed issue, e.g.
  `Closes #89`, `Fixes #90`, `Resolves #91`.
- Do not rely on manual post-merge issue triage when the work is already done
  in the PR; encode closure directly in the PR body.
- PRs must include an explicit exit-steps checklist item confirming issue status
  updates are complete before merge.
- AI agents and human contributors follow the same requirement. If issue status
  closure is missing, the PR is not done.

---

## I-10: Do not edit CHANGELOG.md manually

`CHANGELOG.md` is updated automatically by the release workflow (semantic
release / release tooling). Agents and contributors must **not** add,
reorder, or edit changelog entries in feature, bugfix, or docs PRs.

What to do instead:

- Describe user-visible changes in the PR title and body.
- Rely on conventional commit messages and the release process to populate
  `CHANGELOG.md` after merge.

If release tooling fails to capture a change, fix the release configuration or
commit message conventions — do not patch `CHANGELOG.md` by hand in ordinary
PRs.

---

## I-11: Explain technical concepts in plain language, example-first

When explaining a concept, design, trade-off, or change to the maintainer, lead
with plain language and a concrete, user-facing example — not internal function
names and call graphs.

Rules:

- **Anchor on what the user sees.** Show a real model definition and the SQL (or
  behavior) it produces, then explain the internal mechanics against that
  anchor. "A `datetime` field becomes `timestamptz` — here's the `CREATE TABLE`"
  beats "`canonical_from_parts` maps the logical type."
- **Introduce jargon only after the plain version.** Function and type names are
  precise back-references once the idea is clear — not the primary explanation.
- **Use analogies for architecture.** "Two translation dictionaries that must
  agree by hand" communicates a duplication smell faster than a module diagram.
- **Show, don't just tell.** Prefer before/after diffs, rendered SQL, and
  concrete values over abstract prose (complements the show-don't-tell habit
  used in issue/PR explanations).

This applies to brainstorming, design discussions, PR descriptions, issue
comments, and any explanation directed at the maintainer. It governs how work is
communicated, not what gets built.

---

## I-13: Query and update literals match save() for datetime/date/time/UUID/Decimal/secrets

A developer pages a chat transcript with `after()` on a UTC datetime order
key:

```python
pinned_at = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
await Message(id=2, pinned_at=pinned_at, ...).save()
page = await (
    Message.order_by(lambda message: message.pinned_at)
    .order_by(lambda message: message.id)
    .after((pinned_at, 2))
    .all()
)
```

SQLite stores that timestamp as **text**. `save()` writes pydantic JSON
(`2026-03-01T15:00:00Z`). `datetime.isoformat()` writes `…+00:00`. Those are
the same instant to a human and to Postgres `timestamptz`; to SQLite they
are different strings, so keyset prefix equality misses and the next page
can skip the cursor row.

Query and `update()` literals for `datetime`, `date`, `time`, UUID, and
Decimal canonicalize through one function (`canonicalize_wire_scalar`) whose
output for a given value is pinned against `save_bind_payload`. `save()`
remains instance `model_dump` (field serializers stay on that door). A
second datetime/UUID/Decimal cascade — including `datetime.isoformat()` or a
“mirrors save_bind_payload” comment — is an I-13 violation. Enums on the
query wire are out of scope for this invariant.

A `SecretStr` / `SecretBytes` is unwrapped first, through `unwrap_secret`,
on every door: `save()` binds the real value, a query or `update()` literal
binds the same value, and the display mask `**********` never becomes a
bind, a schema default or a hydrated value (#760; review I-38 in #786). A
second unwrap, a JSON dump of a secret column or a `SecretBytes` column
typed as text is an I-13 violation.

Pinned by helper-vs-save unit equality for those five types and by a SQLite
`save` then `after((original_python_datetime, pk))` paging test.
Architecture review: I-22 in #428. See PRD #430 and
`docs/solutions/patterns/wire-scalar-canonicalization.md`.

---

## I-14: Docstrings are short Google-style reference entries

The API reference is rendered from docstrings by mkdocstrings and griffe, so
a docstring is a reference entry, written once in one form:

```python
async def get(cls, pk: Any, *, session: Session | None = None) -> Self:
    """Fetch one record by primary key.

    Args:
        pk: The primary key value to look up.
        session: The session to run in. Defaults to the one opened by the
            enclosing `async with engines.session():`.

    Returns:
        The record whose primary key is `pk`.

    Raises:
        ModelDoesNotExist: No row has that key. Use
            [`get_or_none`][ferro.Model.get_or_none] to get `None` instead.

    Examples:
        ```python {test=run prelude=reference}
        author = await Author.get(1)
        print(author)
        ```
    """
```

Rules:

- **Google style as griffe parses it**: `Args:`, `Returns:`, `Yields:`,
  `Raises:`, `Attributes:`, `Examples:`, each a section griffe recognises,
  with entries indented under it. A section griffe drops or splits into
  table rows is a bug in the docstring.
- **The summary is one line ending with a period.** A function or method
  summary is an infinitive ("Fetch one record by primary key."); a class
  summary is a noun phrase ("A lazy query over one model's rows."). The
  body starts after a blank line.
- **Every parameter is in `Args:`**, keyword-only ones included
  (`session`, `using`, `nulls`), with the default stated when it is not
  obvious from the signature.
- **Every public dataclass or Pydantic field** has a one-line docstring: a
  string literal on the line below the field.
- **Cross-references are Markdown autorefs**:
  `` [`Model.where`][ferro.Model.where] `` or `[Model][ferro.Model]`.
  Sphinx roles (`:class:`, `:func:`, `:meth:`, `:data:`) render as literal
  text.
- **Every reference is one the reader can follow.** Docstrings carry no ADR
  numbers, issue numbers, internal codenames or Rust internals (crate,
  function and FFI names); a link to a docs page carries the reasoning.
- **Examples use lambda predicates (I-8) against the shared reference
  prelude** (`docs/examples/_preludes/reference.py`: `Author` and `Post`,
  connected, inside a session), in a fence marked
  `{test=run prelude=reference}` so the docs harness
  (`tests/test_docstring_examples.py`) runs it.
- **An explanation longer than one paragraph lives in a guide or concepts
  page**, and the docstring links to it.

`docs/solutions/patterns/docstring-conventions.md` shows each rule as a
before/after pair. Page-level conventions (running examples, snippet
markers, the session wrapper, the Annotated idiom for I-7 twins) are in
`docs/solutions/patterns/docs-page-conventions.md`.

---

## I-15: Statement presentation is one module

A developer turns the log on and saves a token:

```python
logging.getLogger("ferro.sql").setLevel(logging.DEBUG)

await User(id=1, token=SecretStr("sk_live")).save()
# ferro.sql DEBUG INSERT INTO "user" (...) VALUES ($1, $2) params=(1, ***) 0.4ms

print(await User.where(lambda user: user.token == "sk_live").all.sql())
# SELECT ... WHERE "user"."token" = $1    params=(***,)
```

Two doors, one statement, the same stars. A statement the user can see, a
`ferro.sql` record or a terminal's `RenderedSql`, is built from one
presentation: the `sql`, the `params` after one `masked_params` in
`ferro._sql_log`, and the `dialect`. The engine decides which positions are
secret, once, from the operation's secret-value set (ADR-0057); Python paints
the stars once.

The secret-value set a write or a raw statement hands the engine is one
collector, `deferred_secret_values`, computed on its first read and the same
on every read after it. The engine reads it only when the log is on, and may
read it once per statement the operation sends; a single-shot iterable here
leaves the second statement with an empty set and a secret in the log, with
no error (#774 F13).

A second `(params, mask)` to `REDACTED` zip, or a secret-value iterable a
second statement can exhaust, is an I-15 violation. A new door (an instance
`save().sql()`, a docs capture, a structured handler) calls `masked_params`
and passes the collector. Review I-37 in #786.

---

## I-16: A write lists columns in model field declaration order

```python
await customer.save(only={"updated_at", "name"})
# UPDATE "customer" SET "name" = ?, "updated_at" = ? WHERE "id" = ?
```

`only=` and `exclude=` are sets, and a fetched instance's `__dict__` follows
the `SELECT`. Neither order reaches the statement: `save`, `bulk_create` and
`save(only=)` / `save(exclude=)` emit their INSERT and SET columns in
`model_fields` order, walked in `save_bind_payload`. A set is membership
only.

One call is one statement, in the log and in a docs `sql` block. A column
order that changes between processes or with the row's fetch is an I-16
violation, the mutation-side twin of the rendered-statement pin in I-1
(#776; review I-39 in #786).

---

## Agent skills

### Issue tracker

GitHub Issues on `syn54x/ferro-orm`; external PRs are a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `GLOSSARY.md` at repo root, ADRs in `docs/adr/`. See `docs/agents/domain.md`.

<!-- sdd-routing -->
## Agent workflow

- Specs and plans live in GitHub Issues. Never create `docs/plans`, `docs/specs`, `.scratch/*/issues` or any plan file in the repo.
- Plan with `/grill-with-docs` → `/to-spec` → `/to-tickets`. Build with `/build-epic <epic#>` (M/L/XL) or `/implement <N>` (one ticket).
- Do not use brainstorming, writing-plans, ce-plan or any other plan-writing skill. `/to-spec` owns the spec; `/to-tickets` owns the plan.
- Workers: TDD, run the issue's **Verify** block, open a PR with `Closes #N`, never merge.
- Readiness = open + not blocked + unassigned + `ready-for-agent`. Claim by assigning yourself. Unclaim if you stop without a PR.
- Progress is one comment per issue under `<!-- sdd-progress -->`, rewritten in place. Never post a second one.
- Mode: org. Sizes: `size:S/M/L` labels. Priority: none.
- Upstream feedback: detailed. Unless off, `close-epic` may propose skill-defect issues on syn54x/skills-plus-plus, the profile's allowlisted fields only, each one shown and approved by a human before filing; never from a non-interactive run.
<!-- /sdd-routing -->
