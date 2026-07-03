---
title: "Fable Fixes roadmap"
type: strategy
status: draft
date: 2026-07-02
origin: 2026-07-02 adversarial architecture review (Fable)
---

# Fable Fixes roadmap

Remediation program for the findings of the 2026-07-02 architecture review.
Finding numbers (F1–F15) refer to that review. Structure and process rules
mirror the IR-first roadmap (`2026-06-19-001-ir-first-roadmap.md`): every epic
gets a GitHub epic issue with native sub-issues on Project #7, a milestone
(`FF-<epic>`), and a migration-impact assessment per sub-task. AGENTS.md I-6
applies: each item below is scoped as the long-term fix, not a mitigation.

## Goal

Close the correctness, parity, and performance gaps that must not survive
into 1.0: silent-mutation bugs at the query surface, cross-emitter divergence
for derived types on Postgres, a heuristic hot path, and unbounded identity-map
state.

## Sequencing at a glance

| Order | Epic | Why this position |
|---|---|---|
| 1 | **FF-A** Mutation-surface correctness & typed errors | Public *behavior* changes — breaking after 1.0, cheap now. No dependencies. |
| 2 | **FF-B** Structural I-1 on Postgres | Completes the IR-first program's own promise; blocks trustworthy 1.0 migrations. |
| 3 | **FF-C** Compiled hot path | Largest epic; makes the performance pitch honest. Unblocks FF-D. |
| 4 | **FF-D** Identity map & routing redesign | Rides the v0.14 shim-removal cutover (IR-P9); depends on FF-C's codec plan for refresh-on-load. |
| ∥ | **FF-E** Registry & model identity | Independent; can run parallel to FF-B/FF-C. |
| ∥ | **FF-F** Query builder 1.0 shape | Independent of FF-A–FF-D; gates the public roadmap features (aggregations, partial selects). |
| ∥ | **FF-G** Hardening & hygiene | Small parallel items; fold into whichever epic touches the same file. |

Interaction with the IR-first program: FF-B extends IR-P8.5's consolidation to
the *derived*-type domain; FF-D's global-map removal belongs in the IR-P9 /
v0.14 cutover. Neither replaces those phases — they sharpen their exit gates.

---

## Epic FF-A — Mutation-surface correctness & typed errors

Findings: **F1** (limit/offset silently ignored on update/delete), **F6**
(`create()`/`save()` are silent upserts), **F7** (no exception taxonomy).

**Objective:** no mutating operation may silently do more (or different) work
than the user asked for, and every database failure is catchable by type.
These are behavioral changes to the public API — they must land before 1.0.

**Sub-tasks**

- [x] **A1 — Reject `limit`/`offset` on mutating queries.**
      `Query.update()` / `Query.delete()` raise `ValueError` when `_limit` or
      `_offset` is set (portable SQL has no `DELETE ... LIMIT`; failing loud is
      the long-term design, not a placeholder for implementing it). Remove
      `limit`/`offset` from the QueryIR payload for mutating operations so the
      contract cannot regress. Tests: builder-level raise + static contract on
      the mutating payload shape. *(F1 — the review's most severe finding.)*
      Migration impact: **breaking** (previously silently ignored).
- [x] **A2 — Typed exception hierarchy.**
      `ferro.exceptions` grows a DBAPI-shaped tree
      (`FerroError` → `OperationalError`, `IntegrityError` →
      `UniqueViolationError` / `ForeignKeyViolationError` /
      `NotNullViolationError` / `CheckViolationError`, `DataError`,
      `InterfaceError`). Rust maps `sqlx::Error::Database(e).kind()` to these
      instead of `PyRuntimeError(format!(...))` across all of `operations.rs` /
      `migrate.rs`. Original driver message preserved on the exception.
      Migration impact: **minor** (RuntimeError → subclass; document).
- [x] **A3 — `create()` is a real INSERT.**
      Drop `ON CONFLICT DO UPDATE` from the `create()` path; a duplicate PK or
      unique violation raises `UniqueViolationError` (depends on A2).
      Migration impact: **breaking** (previously clobbered the existing row).
- [x] **A4 — `save()` distinguishes INSERT from UPDATE; upsert becomes explicit.**
      Track persistence state explicitly on the instance (today it is implicit
      in the identity map / `__ferro_connection_name`): transient → INSERT,
      persistent → `UPDATE ... WHERE pk = ?`. Add a named upsert surface
      (`Model.upsert(...)` or `save(on_conflict="update")`) for the users who
      relied on the old behavior. Migration impact: **breaking**; migration
      guide entry required.
- [x] **A5 — Docs & migration guide.**
      Mutations guide, exceptions API page, and
      `ir-first-migration-guide.md` entries for A1/A3/A4. Both declaration
      styles per I-7 where models appear; lambda predicates per I-8.

**Exit gate**

- [x] `Query.limit(...).delete()` / `.update()` raise; no mutating payload
      carries limit/offset.
- [x] `create()` on an existing PK raises `UniqueViolationError` on both
      backends; upsert path exists and is tested.
- [x] Full matrix green with exception types asserted (no string matching on
      driver messages anywhere in tests).

---

## Epic FF-B — Structural I-1 on Postgres (derived-type parity)

Findings: **F2** (default `datetime`/`Enum` diverge between Alembic and runtime
on Postgres; sentinel is sqlite-only), **F10** (naming/type vocabulary still
multi-sourced, with real drift in truncation guards and an IR FK-name field no
emitter honors).

**Objective:** one decision table for *derived* type lowering and artifact
naming, consumed by every emitter; the I-1 sentinel exercises it on the
backend where it can actually fail.

**Sub-tasks**

- [x] **B1 — Decide the canonical derived mappings.**
      Design note (docs/solutions/patterns/) settling: plain `datetime` →
      `timestamptz` (runtime is correct; naive-by-default is the classic ORM
      mistake — Alembic bridge moves to `sa.DateTime(timezone=True)`), and the
      default Enum storage (native PG enum vs `varchar` [+ optional check]).
      Whichever way Enum goes, both emitters emit it; the loser's branch is
      deleted, not special-cased. Migration impact: **breaking** for schemas
      created by the divergent emitter — needs explicit guide entries and a
      drift story for existing DBs.
- [x] **B2 — One derived-type decision table, one consumer per side.**
      `ferro-ddl-lowering::canonical_from_parts` becomes the sole authority for
      `(logical_type, format) → storage`. The Alembic bridge derives
      `_sa_type_from_ir_column` from it mechanically: either a `_core` FFI
      helper (`resolved_db_type_token(logical_type, format, dialect)`) that
      Python maps to SA types 1:1, or a generated vector file asserted
      exhaustively (every pair, both dialects) — not a hand-maintained second
      dictionary pinned by sampled tests.
- [x] **B3 — Single-source artifact naming.**
      `ir/compiler.py`'s `_single_index_name` / `_single_unique_name` /
      `_composite_*` / `_fk_name` stop being reimplementations: expose the
      `ferro-ddl-lowering` helpers through `_core` and call them from the
      compiler. Fixes the live drift: Python's single-unique/index/fk names
      have **no 63-char truncation guard**, Rust's do — >63-char identifiers
      currently get different names per path.
- [x] **B4 — Make IR `foreign_keys[].name` honored or delete it.**
      Today no emitter uses it (runtime FKs are anonymous inline; Alembic's are
      unnamed) — the audit's finding-#4 pattern reborn. Either both emitters
      emit the named constraint (unlocks the AGENTS.md "planned" `fk_`/`pk_`
      rows) or the field is dropped from SchemaIR v1.
- [x] **B5 — I-1 sentinel on the Postgres matrix with a full-type fixture.**
      `test_alembic_autogen_against_rust_migrated_db_is_idempotent` loses
      `sqlite_only`; the fixture model gains `Enum`, `datetime`, `date`,
      `time`, `UUID`, `Decimal`, `bytes`, and JSON fields. Existing filters
      (`_is_pk_nullable_relaxation`, `_is_redundant_single_column_unique`) are
      resolved or explicitly re-justified — the single-column unique shape
      divergence (docs/solutions/issues/sa-vs-rust-unique-constraint-shape.md)
      gets fixed here (Option A: Rust emits named `uq_` constraints), not
      re-filtered.
- [x] **B6 — Delete the remaining hand-mirrors.**
      `alembic.py::_render_check_body` (mirror of
      `ferro_ddl_lowering::render_check_body`) and `_db_type_to_sa_type`'s
      duplicated vocabulary collapse into the B2 mechanism.

**Exit gate**

- [x] Grep-verified: no Python-side reimplementation of naming or derived-type
      lowering (parity tests demote to regression sentinels over shared code —
      the IR-P8.5 exit-gate language, now true for the derived domain too).
- [x] Postgres sentinel green with the full-type fixture and zero filters
      hiding type-family diffs.

---

## Epic FF-C — Compiled hot path (codec plans, catalog cache, native decode)

Findings: **F4** (1–3 catalog queries per operation), **F5** (heuristic codec;
`pattern_looks_decimal` misclassifies `str` fields with numeric patterns),
**F8** (Postgres coerced to "SQLite-shaped text" via `CAST(... AS text)`
projection), **F14/17-part** (`_fix_types` Python-side enum pass).

**Objective:** all per-column type decisions are made **once per model per
schema epoch**, not per value per row; Postgres decodes natively; CodecIR
finally gets its runtime consumer.

**Sub-tasks**

- [x] **C1 — Per-model `ColumnCodec` plan.**
      At registration, compile SchemaIR into `Vec<ColumnCodec>` per model — one
      authoritative enum (`Int/SmallInt/BigInt/Float/Bool/Str/Bytes/Uuid/
      DateTime/Date/Time/Decimal/Json/Enum{values, storage}`), consumed by
      bind (`schema_bind_expr`), fetch decode (`decode_engine_value`), and the
      SELECT projection. Delete `pattern_looks_decimal`, `json_type`, `format`
      sniffing from `codec.rs`. This *is* the CodecIR runtime consumer the RFC
      promised — wire the plan shape to `CodecIrPayload` and the golden vectors.
      Fixes the F5 bug: `year_code: str = Field(pattern=r"^\d{4}$")` currently
      hydrates as `decimal.Decimal` and binds `CAST(... AS numeric)`.
      Regression test for exactly that model first (TDD red).
- [x] **C2 — Schema-epoch catalog cache.**
      `postgres_enum_udt_by_column` / `postgres_uuid_column_names` /
      `postgres_temporal_cast_by_column` results cached on `EngineHandle` keyed
      by table, invalidated in `refresh_pool()` (the existing epoch primitive —
      no new invalidation concept). Then shrink the need: everything except
      Alembic-created native enums is statically derivable from the C1 plan;
      the catalog is consulted once per table per epoch at most.
      *(Landed with one correction: plan-derivation is unsound for **all
      three** type families, not just enums — Postgres has no
      assignment-context coercion for typed parameters, so Alembic-created
      uuid/temporal columns on model-declared `str` fields need catalog-driven
      casts too. The three lookups collapse into one combined `pg_catalog`
      probe per table per epoch instead; probe evidence in
      `2026-07-02-004-ff-c-c2-catalog-cache-design.md`.)*
- [x] **C3 — Native typed decode on Postgres.**
      Add typed `EngineValue` variants via sqlx features (`uuid`, `chrono`,
      `rust_decimal`); remove `apply_postgres_text_select_columns` and the
      `CAST(col AS text)` projection. Kills the session-`TimeZone` rendering
      coupling for `timestamptz` reads and the per-query projection machinery.
      SQLite becomes the emulation target, not the source of truth. Careful
      parity tests on hydrated Python values across backends (aware/naive
      datetimes, Decimal precision, UUID casing).
- [x] **C4 — Enum decode moves into hydration; delete `_fix_types`.**
      `enum_values` live in the plan; Rust hydrates the Python `Enum` member
      directly (registry holds the enum class or Python converts via a single
      plan-driven hook). Removes the partial-coverage, `except Exception: pass`
      Python post-pass from every fetch (I-6).
- [x] **C5 — Benchmarks.**
      A small pinned benchmark suite (save, filtered fetch ×10k rows, both
      backends) run before/after C1–C3 so the "high-performance core" claim is
      measured, and future regressions on the hot path are visible.
      Landed in `benchmarks/` (single/bulk save + filtered fetch ×10k, SQLite +
      Postgres, rich-type fixture; `just bench` runner, checked-in per-backend
      baselines, `benchmarks/compare.py` for before/after deltas).

**Exit gate**

- [x] Zero catalog queries on steady-state CRUD (verified by a statement-count
      test against a live PG).
- [x] No JSON-schema shape/pattern inference anywhere in `codec.rs`.
- [x] F5 repro model round-trips as `str` on both backends.
- [x] Benchmarks recorded in-repo; full matrix green.
      (`benchmarks/README.md` § FF-C epic results: Postgres −76% to −91%
      pre-C1 → post-C2; matrix 1044 passed.)

---

## Epic FF-D — Identity map & routing redesign (v0.14 cutover)

Findings: **F3** (unbounded strong refs, stale reads, global-clear
invalidation), **F13** (triple stringly-typed route resolved twice; `using=`
silently bypasses ambient session; `Model.__init__` reads target PK by the
*source* model's PK name).

**Objective:** identity is session-scoped, memory-bounded, and never returns
stale data; routing is resolved exactly once through one handle. Lands with
the IR-P9 / v0.14 shim removal, which already deletes the ambient-global path.

**Sub-tasks**

- [ ] **D1 — Weak-value identity map with refresh-on-load.**
      Map holds weak references (instances are released when user code drops
      them). On fetch-hit, update the cached instance's `__dict__` from the
      freshly fetched row (the decoded fields are already in hand — today they
      are discarded), preserving `a is b` while eliminating the stale-read
      class. Document the guarantee precisely in
      `docs/pages/concepts/identity-map.md`.
- [ ] **D2 — Scoped invalidation.**
      Rollback evicts per `(connection, session)` — not the global
      everything-clear in `rollback_transaction`; bulk update/delete evict per
      `(connection, model)`. Global `IDENTITY_MAP` is deleted with the v0.14
      ambient-session removal (IR-P9), not kept as a fallback.
- [ ] **D3 — One route handle.**
      Replace the `(tx_id, using, session_id)` triple threaded through every
      FFI call with a single opaque route resolved once (in
      `resolve_operation_scope`) and passed through. Rust stops re-deriving
      the connection per operation (`active_route_for_operation` collapses).
- [ ] **D4 — `using=` vs ambient session is an error.**
      The silent session-bypass in `resolve_operation_scope` (explicit `using`
      different from ambient session → runs sessionless on the global map)
      becomes a `ValueError` at the v0.14 boundary. Migration impact:
      **breaking**; deprecation warning lands ahead of the cutover.
- [ ] **D5 — Fix `Model.__init__` FK extraction.**
      Relationship inputs read the *target* model's PK field name, not the
      source's (`models.py:197–204`). Correct today only because every model's
      PK is named `id`. Test with a target model whose PK is not `id`.

**Exit gate**

- [ ] A loop loading 1M rows shows bounded RSS after GC (memory test).
- [ ] External UPDATE + re-fetch returns fresh values with identity preserved.
- [ ] Exactly one route-resolution site per operation (grep-verified);
      v0.14 matrix green with the global map gone.

---

## Epic FF-E — Registry & model identity

Findings: **F9** (bare-class-name registry keys silently clobber; table name
not configurable), **F11** (O(N²) import-time SchemaIR recompiles;
`resolve_relationships` swallows schema failures).

**Sub-tasks**

- [ ] **E1 — Qualified registry keys + duplicate detection.**
      Python and Rust registries key by qualified name; a second model
      resolving to the same *table name* raises at class definition. FK
      string resolution follows the same rules (unambiguous short name still
      works; ambiguity errors with candidates listed).
- [ ] **E2 — Configurable table name.**
      `__ferro_table__` (or `model_config` key) overriding the
      `classname.lower()` default. SchemaIR already carries `table_name`
      separately — cheap now, breaking to retrofit after 1.0.
- [ ] **E3 — Kill the O(N²) import cost.**
      `_generate_and_register_schema` stops calling
      `compile_registry_schema_ir()` per class; the registry modelset compiles
      lazily (connect/create_tables/migrate/get_metadata already push it).
      Add an import-time budget test (N models → O(N) schema builds).
- [ ] **E4 — `resolve_relationships` fails loudly.**
      Remove the `except Exception: pass` around re-registration; a model
      whose schema fails to rebuild aborts with the model named (I-6).

**Exit gate**

- [ ] Two same-named models in different modules: hard error, actionable
      message. Custom table names round-trip through both emitters (parity
      test). Import of 200-model fixture within budget.

---

## Epic FF-F — Query builder 1.0 shape

Finding: **F12** (mutable aliasing, no column validation, `Any`-typed proxies,
deprecation bookkeeping inside the AST, QueryIR lowered through the legacy
`QueryDef`). Gates the public roadmap features (aggregations, partial selects,
eager loading) — build those on the post-F shape, not the current one.

**Sub-tasks**

- [ ] **F-1 — Immutable chaining.**
      `where`/`order_by`/`limit`/`offset` return a new `Query`;
      `q2 = q1.where(...)` no longer mutates `q1`. `first()` stops temporarily
      mutating `self._limit`. Migration impact: **minor** (code that relied on
      aliasing is almost certainly buggy already; document).
- [ ] **F-2 — Column-name validation at build time.**
      `QueryProxy`/`order_by` validate attribute names against
      `model_fields` + shadow columns; `lambda user: user.nmae == 3` raises
      `AttributeError` naming valid columns. Deletes the documented
      "`order_by` lambda produces a junk column — never show it" trap by
      making it impossible.
- [ ] **F-3 — Typed proxies via `@dataclass_transform`.**
      Per-field `FieldProxy[T]` through the metaclass so
      `lambda user: user.age >= "x"` fails type-checking — the plumbing the
      nodes.py docstring already names as the real design.
- [ ] **F-4 — Collapse `QueryDef` onto the IR types.**
      `operations.rs`/`query.rs` consume `ferro_schema_ir::QueryNode` directly;
      delete the IR→legacy conversion layer (`query_node_from_ir`, the
      `QueryDef` shadow shapes). Prerequisite for extending the payload with
      projection/aggregation nodes without triple-editing.
- [ ] **F-5 — Post-operator-style surface decision.**
      Decide v1.0 `order_by` (string / lambda / `col()`); if `FieldProxy`
      class-attribute injection is no longer needed once operator style is
      removed (v0.14), delete it — restoring normal Pydantic class-attribute
      semantics (`User.age` stops being a `FieldProxy`).
- [ ] **F-6 — Then, and only then: aggregations + partial selects** (the
      existing public-roadmap items), designed as IR payload extensions with
      hydration-ABI-aware partial materialization.

**Exit gate**

- [ ] Builder is immutable; misspelled columns fail at build time; QueryIR is
      the only query shape in Rust (grep: no `struct QueryNode` outside the IR
      crate); typed-predicate static tests added to `test_static_contracts.py`.

---

## Epic FF-G — Hardening & hygiene

Findings: **F14**, **F15** and review "Low" items. Small, parallelizable;
fold into whichever epic touches the same file when convenient.

**Sub-tasks**

- [ ] **G1 — Hydration ABI structural guard.**
      At `_core` import, diff `BaseModel.__slots__` against the slots the
      hydrator initializes; unknown slot → loud, actionable startup error
      (turns "breaks on next Pydantic minor" into "refuses to start").
      Fix the swallowed `let _ = instance.setattr(__pydantic_fields_set__, …)`
      to propagate.
- [ ] **G2 — `operations.rs` dedup.**
      One `ModelMeta` (pk name, autoincrement, table) resolved per operation
      replaces the six copy-pasted PK-discovery scans; an executor abstraction
      over `Option<TransactionConnection>` replaces the doubled tx/no-tx match
      arms. Behavior-preserving; shrinks the god-module ahead of FF-C/FF-D
      work in the same file.
- [ ] **G3 — Transactional auto-migrate on Postgres.**
      Wrap each table's plan (or the whole run) in a transaction so a mid-run
      failure cannot leave partial DDL (Postgres DDL is transactional; SQLite
      documented as best-possible per its capabilities — scoped-down, stated).
- [ ] **G4 — Small correctness edges.**
      `save()`'s `(id > 0).then_some(id)` PK heuristic (breaks legitimate
      non-positive PKs); second unnamed `connect()` silently replacing the
      default engine → error; document/guard the identity-map DashMap+GIL
      lock-order hazard (all access must hold the GIL) with a debug assertion.
- [ ] **G5 — `RustValue::into_py_any` module-handle caching.**
      Intern `datetime`/`uuid`/`decimal`/`json` handles instead of
      `py.import` per value. Micro; measure under FF-C's benchmarks.
- [x] **G6 — Idempotent check-constraint emission in Postgres auto_migrate.**
      `db_check=True` fields emit their check constraint via a non-idempotent
      `ALTER TABLE ... ADD CONSTRAINT` in `post_create_sqls`
      (`ferro_ddl_lowering::render_db_check`, Postgres arm), sitting alongside
      the idempotent `CREATE TABLE IF NOT EXISTS`. A second
      `connect(auto_migrate=True)` against an already-migrated schema (e.g. an
      app restart) fails with `constraint "ck_..." ... already exists`.
      Discovered incidentally during FF-A/A2 validation
      ([#176](https://github.com/syn54x/ferro-orm/issues/176)). Fix by making
      emission idempotent — a guarded `DO $$ ... IF NOT EXISTS` block or a
      `pg_constraint` existence check during the create pass, mirroring how
      live introspection already checks — not by swallowing the error (I-6).
      Migration impact: **none** (internal migration-path fix; a
      previously-failing second `connect()` now succeeds).

**Exit gate**

- [ ] Slot-guard test that fails when a fake slot is injected; `cargo llvm-lines`
      (or LOC) shows `operations.rs` duplication removed; PG migration
      interrupted mid-plan leaves the schema unchanged.
- [x] Second `connect(auto_migrate=True)` against an already-migrated Postgres
      schema with a `db_check` model succeeds.

---

## Verification commands (program-level)

- `cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate`
- `cargo test --no-default-features --features testing`
- `uv run pytest -q`
- `uv run pytest -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres -q`
- `uv run pytest tests/test_cross_emitter_parity.py tests/test_db_type_cross_emitter_parity.py -q` (post-FF-B: includes the Postgres full-type sentinel)

## Risk register

- Risk: FF-B's enum/datetime decision changes DDL for existing users' schemas.
  - Mitigation: decision lands with drift detection that warn-and-skips (the
    #154 pattern) plus explicit migration-guide recipes; never silent ALTERs.
- Risk: FF-C native decode (C3) shifts hydrated value shapes subtly
  (aware datetimes, Decimal precision).
  - Mitigation: cross-backend hydration-equivalence tests written *before* the
    decode swap; shadow-compare hydrated values during the transition the same
    way migrate plans were shadow-compared.
- Risk: FF-A/FF-D breaking changes stack up with the v0.14 cutover.
  - Mitigation: FF-A ships early behind its own minor release with deprecation
    warnings where feasible (A1 cannot warn — it must break; call it out at
    the top of the release notes). FF-D is explicitly part of v0.14.
- Risk: epics touching `operations.rs` (A, C, D, G2) conflict.
  - Mitigation: land G2 (dedup) before FF-C/FF-D start; FF-A's Rust surface is
    small and lands first anyway.
