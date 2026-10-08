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
    drops data or can fail on them. Every door reads that one verdict and
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
      One normalizer, `normalize_check_definition`, compares the canonical
      rendering with the catalog's; it folds only the casts Postgres adds for
      display (any cast on a literal, and a text-family cast on a column or
      `ARRAY[…]`), and every other cast is drift (#563).
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

    Every artifact above reaches every door through the one planner,
    `ferro_migrate::plan_from_ir` (`_core._plan_from_ir` over FFI): its
    per-family deciders are private to it and pinned through it, never as a
    second entry point; `_core._plan_enum_type_provenance` remains as a
    pass-side parity pin. The item is carried by pins **(e)** and **(f)**
    below.

### The seven pins

`tests/test_cross_emitter_parity.py` pins the migrations door, the bridge and
the pass itself against the one planner over every casebook change
(`tests/_casebook.py`, cases A–F, built from the generator tests' own models)
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
  built from the same models, and the same server default on every column
  (no backfill `DEFAULT` left behind, and a renamed table's serial sequence
  under the name a fresh table gives it), and Alembic autogenerate against
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
  plan's reports by kind and subject, never by sentence.

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
  against an auto-migrated database is empty) and the seven pins; `tests/test_db_type_cross_emitter_parity.py` pins every type token;
  `tests/test_alembic_autogenerate.py` and `tests/test_schema_constraints.py`
  pin the names (`test_index_name_matches_rust_runtime_*`).
- `docs/solutions/patterns/cross-emitter-ddl-parity.md` documents the rule and
  the recipes below.

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
3. Add a casebook case (`tests/_casebook.py`) so pins (a)–(g) cover it, and a
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

## I-8: Lambda predicates are the official query style

Documentation and examples use the lambda predicate style for all queries:

```python
adults = await User.where(lambda user: user.age >= 18).all()
```

Rules:

- **Every query example** in docs, docstring `Examples:` sections, and
  `docs/examples/` scripts uses lambda predicates.
- Name the lambda parameter after the model in **lowercase singular**
  (`user` for `User`, `post` for `Post`) so filters read naturally.
- When the predicate styles themselves are documented, present them in
  order **lambda > `col()` > operator**, with lambda labeled the officially
  recommended style.
- **Operator style** (`User.where(User.age >= 18)`) is compatible today but
  is slated for deprecation in a future release and fails static type
  checking (`User.age >= 18` types as `bool`; `where()` expects
  `QueryNode | Predicate`). Docs say so explicitly wherever the style is
  shown.
- **`order_by` is not a predicate**, but its lambda selector
  (`order_by(lambda u: u.age, "desc")`) is validated and is the documented
  style — and it is the ONLY way to order by a related column
  (`order_by(lambda t: t.account.label)`), which relation traversal requires.
  Attribute style is not accepted — `order_by` takes a column-name string or
  a lambda selector (anything else raises `TypeError`).

The canonical comparisons live in `docs/pages/guide/queries.md`
("Predicate Styles") and `docs/pages/concepts/query-typing.md`; everywhere
else uses lambda without restating the trade-offs.

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

## I-13: Query and update literals match save() for datetime/date/time/UUID/Decimal

A developer pages a chat transcript with `after()` on a UTC datetime order
key:

```python
pinned_at = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
await Message(id=2, pinned_at=pinned_at, ...).save()
page = await (
    Message.order_by(lambda m: m.pinned_at)
    .order_by(lambda m: m.id)
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

Pinned by helper-vs-save unit equality for those five types and by a SQLite
`save` then `after((original_python_datetime, pk))` paging test.
Architecture review: I-22 in #428. See PRD #430 and
`docs/solutions/patterns/wire-scalar-canonicalization.md`.

---

## Agent skills

### Issue tracker

GitHub Issues on `syn54x/ferro-orm`; external PRs are a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` at repo root, ADRs in `docs/adr/`. See `docs/agents/domain.md`.

<!-- sdd-routing -->
## Agent workflow

- Specs and plans live in GitHub Issues. Never create `docs/plans`, `docs/specs`, `.scratch/*/issues` or any plan file in the repo.
- Plan with `/grill-with-docs` → `/to-spec` → `/to-tickets`. Build with `/build-epic <epic#>` (M/L/XL) or `/implement <N>` (one ticket).
- Do not use brainstorming, writing-plans, ce-plan or any other plan-writing skill. `/to-spec` owns the spec; `/to-tickets` owns the plan.
- Workers: TDD, run the issue's **Verify** block, open a PR with `Closes #N`, never merge.
- Readiness = open + not blocked + unassigned + `ready-for-agent`. Claim by assigning yourself. Unclaim if you stop without a PR.
- Progress is one comment per issue under `<!-- sdd-progress -->`, rewritten in place. Never post a second one.
- Mode: org. Sizes: `size:S/M/L` labels. Priority: none.
- Upstream feedback: on. When on, `close-epic` may propose skill-defect issues on syn54x/skills-plus-plus, allowlisted fields only, each one shown and approved by a human before filing; never from a non-interactive run.
<!-- /sdd-routing -->
