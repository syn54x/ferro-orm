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
3. **Column types** — decided by ONE function:
   `ferro_ddl_lowering::resolve_column_storage` (explicit `db_type` token →
   native-enum resolution → the `canonical_from_parts` cascade). The Alembic
   bridge consumes it mechanically over FFI (`_core._resolve_storage_type`)
   and `_db_type_to_sa_type` is only the SA *rendering* of the shared token
   vocabulary — never a second decision table. Pinned exhaustively by
   `tests/test_db_type_cross_emitter_parity.py` (every token and every
   derived annotation × both dialects). See
   `docs/solutions/patterns/derived-type-and-naming-decision-table.md`.
4. **Index names** — `idx_<table>_<col>` for single-column indexes,
   `idx_<table>_<col1>_<col2>...` for composite indexes.
5. **Unique constraint names** — `uq_<table>_<col>` for single-column,
   `uq_<table>_<col1>_<col2>...` for composite.
6. **Foreign key constraint names** — `fk_<table>_<col>_<to_table>`, always
   emitted (both emitters render `SchemaForeignKey.name`; single-sourced in
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
10. **Nullability** — must agree.
11. **Enum label additions** — the label-addition decision (which model-declared
    labels a live enum type is missing, and which live labels are extra) and
    the rendered `ALTER TYPE ... ADD VALUE IF NOT EXISTS` statement are decided
    by ONE pair of functions: `ferro_ddl_lowering::missing_enum_labels` /
    `extra_enum_labels` + `render_pg_enum_add_value`. The auto-migrate
    reconciliation pass consumes them directly; the Alembic autogenerate
    comparator consumes them over FFI (`_core._plan_enum_label_addition`) and
    executes the byte-identical statements. Pinned by
    `tests/test_cross_emitter_parity.py` and the ferro-ddl-lowering unit pins.
    See ADR-0011 (append-only; update-gated; warn-never-act for extras).
12. **Check additions** — the missing-check decision (which declared `ck_*`
    names — table checks, then column checks — a live table does not carry)
    and the rendered ADD are decided by ONE pair of functions:
    `ferro_ddl_lowering::missing_check_names` / `render_check_addition`.
    The auto-migrate reconciliation pass consumes them through
    `ferro_migrate::plan_missing_checks`; the Alembic autogenerate
    comparator consumes them over FFI (`_core._plan_check_addition`) and
    executes the byte-identical statements. Pinned by
    `tests/test_table_check_reconcile.py`. See ADR-0013 (add on
    `migrate_updates`; name-only comparison) and ADR-0014 (SQLite warn-skip).
13. **Check rebuilds** — the same-name body-drift decision (which declared
    `ck_*` names exist live **and** whose catalog definition normalizes
    unequal to the canonical rendering) and the rendered DROP + bare ADD
    are decided by ONE pair of functions:
    `ferro_ddl_lowering::drifted_check_names` / `render_check_rebuild`
    (one normalizer: `normalize_check_definition`). The auto-migrate
    reconciliation pass consumes them through
    `ferro_migrate::plan_check_rebuilds`; the Alembic autogenerate
    comparator consumes them over FFI (`_core._plan_check_rebuild`) and
    executes the byte-identical statements. Pinned by
    `tests/test_table_check_rebuild.py`. See ADR-0015 (canonical render vs
    catalog, both through one normalizer) and ADR-0014 (SQLite warn-skip).
14. **Check leftovers / drops** — the leftover-name decision (which live
    ferro-owned `ck_*` names a table carries that the model no longer
    declares) and the rendered DROP are decided by ONE trio of functions:
    `ferro_ddl_lowering::extra_check_names` /
    `extra_check_names_warning` / `render_check_drop`. The auto-migrate
    reconciliation pass consumes them through
    `ferro_migrate::plan_check_drops` (ops only under
    `migrate_destructive`; the warning always fires on `migrate_updates`).
    The Alembic autogenerate comparator consumes them over FFI
    (`_core._plan_check_drop`) with **no** destructive gate and executes
    the byte-identical statements. Pinned by
    `tests/test_table_check_orphans.py`. See ADR-0013 (leftover warning +
    destructive ladder) and ADR-0014 (SQLite warn-skip).
15. **Row policy names and DDL** — the live policy name
    (`rls_<table>_<name>`; `name` defaults to the shorthand's column), the
    column/setting shorthand's cast (from `resolve_column_storage`; `uuid`,
    `text`/`varchar` and the integer families only), the rendered
    `<col> = NULLIF(current_setting('<key>', true), '')::<cast>` expression,
    and the full `ALTER TABLE … ENABLE/FORCE ROW LEVEL SECURITY` +
    `CREATE POLICY` statements are decided by ONE family of functions in
    `ferro_ddl_lowering`: `row_policy_name` / `is_ferro_row_policy_name` /
    `row_policy_shorthand_cast` / `render_row_policy_setting_expr` /
    `row_policy_clauses` / `render_create_row_policy` /
    `render_enable_row_security` / `render_force_row_security` /
    `row_security_statements`, over the command table
    `ROW_POLICY_COMMANDS` / `row_policy_command_token` /
    `row_policy_command_takes_using` / `row_policy_command_takes_with_check`.
    The auto-migrate create pass consumes them through
    `ferro_migrate::render_create_table`. The Python declaration surface
    (`src/ferro/rowsecurity.py`) consumes the name, the cast, and the command
    table over FFI (`_core._ddl_row_policy_name`, `_core._rls_shorthand_cast`,
    `_core._rls_command_matrix`) rather than keeping its own copies, so a
    declaration fails at class definition for exactly the columns and clauses
    DDL would fail for. The Alembic autogenerate operation (#414) **will**
    consume the whole emission over FFI (`_core._plan_row_security`; the seam
    and its byte-parity with the create pass are already pinned by
    `test_row_security_statement_parity_pin`). Pinned by
    `tests/test_row_security_create_pass.py` and the ferro-ddl-lowering unit
    pins. Postgres-only; SQLite gets one warning per table and no DDL
    (ADR-0014 posture). See PRD #406.
16. **Row-security reconciliation on a live table** — the whole decision for
    one existing table (which declared policies are missing, which live ones
    drifted, which raw bodies ferro cannot verify, which ferro-owned `rls_*`
    policies are orphaned, which live policies are foreign, the one-way
    `ENABLE`/`FORCE` statements, the `migrate_destructive` teardown, and every
    warning) is decided by ONE function family in `ferro_ddl_lowering`, over
    the same `LiveRowSecurity` / `LiveRowPolicy` input:
    `plan_row_security_reconcile` — built from `missing_row_policy_names` /
    `row_policy_drift` / `extra_row_policy_names` /
    `missing_row_security_flag_statements` /
    `excess_row_security_flag_statements` / `render_drop_row_policy` /
    `row_policy_rebuild_statements`, one normalizer
    (`normalize_row_policy_expr`), one catalog decoder
    (`row_policy_command_from_catalog_code`), and the warning texts
    (`dropped_row_security_warning`, `extra_row_policy_names_warning`,
    `foreign_row_policy_warning`, `unverifiable_row_policy_warning`,
    `row_security_teardown_warning`, `row_security_migrator_warning`). The
    auto-migrate reconciliation pass consumes it directly (`src/migrate.rs`,
    after that table's column and data steps); the Alembic autogenerate
    operation (#414) **will** consume it over FFI
    (`_core._plan_row_security_reconcile`, whose byte-parity with the pass is
    already pinned). Live state comes from `src/introspect.rs`
    (`live_table_row_security`: `pg_class.relrowsecurity` /
    `relforcerowsecurity` plus `pg_policy`, with `ferro_owned` decided by
    `is_ferro_row_policy_name`). Pinned by
    `tests/test_row_security_reconcile.py`,
    `tests/test_row_security_rebuild.py`,
    `tests/test_row_security_orphans.py` and the ferro-ddl-lowering unit pins
    (which carry real `pg_get_expr` fixtures). Postgres-only: SQLite plans
    nothing and keeps the create pass's single warning (ADR-0014). See
    ADR-0019 (rebuild the bodies ferro writes, report the bodies you write;
    flags are one-way) and PRD #406.

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

## Agent skills

### Issue tracker

GitHub Issues on `syn54x/ferro-orm`; external PRs are a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` at repo root, ADRs in `docs/adr/`. See `docs/agents/domain.md`.
