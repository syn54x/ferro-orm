# FF-G Hardening & Hygiene — Design (G1, G3, G4, G5)

**Scope:** Epic FF-G sub-tasks G1, G3, G4, G5 in one PR, branched off `main`
(`eb7fece`, post-FF-F #223). **G2 (`operations.rs` dedup) is explicitly
excluded** and ships in its own follow-up PR — this PR does not refactor the
tx/no-tx `match` arms or the PK-discovery scans (G4a makes one surgical edit
inside two of those arms, nothing more). **G6 is already done** (#176).

**Version context:** `pyproject.toml`/`Cargo.toml` are at `0.13.0`;
semantic-release computes the next version from commit types — no version
files are edited. The two G4 user-observable breaks are marked `!`.

---

## G1 — Hydration ABI structural guard

### Problem

Zero-copy hydration (`src/hydration.rs`) writes Pydantic v2's `BaseModel`
slots directly instead of calling `__init__`. If a future Pydantic minor adds
a slot the hydrator doesn't know about, hydrated instances silently carry an
uninitialized slot — breakage surfaces far from the cause. Separately,
`hydration.rs:110` swallows the `__pydantic_fields_set__` setattr error
(`let _ =`), while its sibling at `:197` correctly propagates with `?`.

### Design

**Single shared slot source.** One const in `src/hydration.rs`:

```rust
/// Every `BaseModel.__slots__` entry the zero-copy hydrator accounts for.
/// The import-time ABI guard diffs live `BaseModel.__slots__` against this
/// list, and `init_handled_slots` is driven by it — the guard and the
/// hydrator cannot drift because they read the same source.
pub(crate) const HANDLED_BASEMODEL_SLOTS: &[&str] = &[
    "__dict__",                    // materialized by field writes
    "__pydantic_fields_set__",     // set from decoded columns
    "__pydantic_extra__",          // per model_config extra policy
    "__pydantic_private__",        // always None (no __init__ ran)
];
```

**The const drives the hydrator.** `set_pydantic_hydration_slots` is
restructured into `init_handled_slots(py, cls, instance, fields_set)` that
iterates `HANDLED_BASEMODEL_SLOTS` and `match`es each name to its
initializer (same logic as today: extra-policy dict-or-None, private None,
fields_set setattr, `__dict__` no-op). A name present in the const with no
match arm hits a fallback arm that returns a loud `PyRuntimeError` naming the
slot — adding a slot to the list without teaching the hydrator fails on
first hydrate instead of silently skipping. Drift in every direction is loud:

| Drift | What happens |
|---|---|
| Pydantic adds a slot | Import-time guard fails, names the slot |
| Slot added to const, no initializer | First hydrate fails, names the slot |
| Initializer added, const not updated | Guard fails at import (slot still unknown) |

**Import-time guard.** `verify_pydantic_slot_abi(py)` in `hydration.rs`,
called from the `#[pymodule] fn _core` init in `src/lib.rs`: import
`pydantic`, read `BaseModel.__slots__`, and error on any slot not in
`HANDLED_BASEMODEL_SLOTS`. (Pydantic is a hard dependency of ferro and is
imported by `ferro/__init__.py` regardless; the guard does not add an
import that wasn't already paid for.) Error text names every unknown slot,
the running Pydantic version, and the remedy:

```
ferro's zero-copy hydration does not know how to initialize pydantic
BaseModel slot(s): '__pydantic_future__' (pydantic 2.12.0). This ferro
build supports: __dict__, __pydantic_fields_set__, __pydantic_extra__,
__pydantic_private__. Your pydantic version is likely newer than this
ferro build — pin pydantic to a supported minor or upgrade ferro.
```

**Testability.** The diff logic is exposed as
`_verify_hydration_abi_for_test(cls)` (pyfunction, underscore-private): the
exit-gate test passes a fake class whose `__slots__` extends
`BaseModel.__slots__` with an injected slot and asserts the error names it.
The import-time guard calls the same function with the real `BaseModel`.

**Swallowed setattr.** `hydration.rs:110` becomes
`instance.setattr(intern!(py, "__pydantic_fields_set__"), fields_set)?;` —
mirroring `:197`. Folded into the `init_handled_slots` match arm.

The module docstring (`hydration.rs:1-6`) and `hydrate_model_instance` docs
are updated to point at the const as the authority.

---

## G3 — Transactional auto-migrate on Postgres

### Problem

`internal_migrate` (`src/migrate.rs:795`) executes each table's plan
statement-by-statement on the pool (`engine.execute_sql_unprepared`). A
mid-plan failure (statement 2 of 3 rejected by the server) leaves the table
half-migrated — partial DDL the user must repair by hand.

### Design — per-table transaction (Postgres)

**Scope decision: per-table, not whole-run.** Each table's plan
(`plan.statements` + drop-column ALTERs) executes inside one transaction via
`engine.begin_transaction_connection()` (`src/backend.rs:631` — commit and
rollback already exist on `EngineConnection`). Failure → rollback +
propagate the existing mapped error. Rationale over whole-run:

- The invariant it buys is the strongest useful one: **every table is either
  fully migrated or untouched** — a failed run is safely re-runnable and
  continues where it stopped.
- Whole-run would roll back earlier tables' already-valid upgrades on an
  unrelated late failure, and holds locks across every table's
  introspection + DDL for the whole pass.
- The create-tables pass that precedes diffing is already idempotent
  (`CREATE TABLE IF NOT EXISTS`) and stays outside.

Postgres drop-column execution needs no introspection (the SQLite index
pre-scan in `execute_drop_column` is SQLite-only), so the Postgres arm
renders the same `ALTER TABLE ... DROP COLUMN` inside the transaction.
The shadow migrate-planner comparator (`migrate.rs:831-842`) runs at
plan time, before execution — untouched and must stay clean under
`FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1`.

**SQLite: documented, not silently claimed equivalent.** SQLite keeps the
current sequential pool execution. The `connect()` / `migrate()` docstrings
state it explicitly: on SQLite, auto-migrate applies statements
individually; a mid-run failure can leave earlier statements applied
(SQLite ALTERs are single-statement operations; the drop-column path
interleaves introspection between statements). Postgres wording: a failed
table migration rolls back that table's plan entirely.

Pool refresh after DDL (`migrate.rs:874-881`) is unchanged — it keys off
`ddl_ran`, which is now set after a table's transaction commits.

### Exit-gate test

Postgres: a model whose migration plan renders ≥2 statements, where a later
statement fails at execution time (e.g. the plan's `CREATE INDEX` name is
pre-created manually with a different definition, so the server rejects it).
Assert the earlier `ADD COLUMN` did **not** persist — live columns are
unchanged after the failed `migrate()`.

---

## G4 — Small correctness edges (three)

### G4a — `(id > 0).then_some(id)` PK heuristic — `fix(ff-g)!`

`src/operations.rs:1344` and `:1365` (the Postgres RETURNING arms of
`save_record`) decode the returned PK as
`.as_i64().unwrap_or(0)` then `(id > 0).then_some(id)` — a legitimate
non-positive PK (sequence with `MINVALUE 0`/negative, or explicit `id=0`
upsert) is discarded; Python's `save()` leaves the instance PK `None`, so
identity-map registration is skipped and a later `save()` raises
`Cannot UPDATE ... without a primary key value`.

**Fix (surgical, two lines per arm):** return the decoded value directly —

```rust
let id = rows
    .first()
    .and_then(|row| row.values.first())
    .and_then(|(_, value)| value.as_i64());
Ok(id)
```

No row / non-integer PK (e.g. UUID) still yields `None`, exactly as today.
**Classification: `fix(ff-g)!`** — it restores correct behavior, but the
observable contract changes (a RETURNING value ≤ 0 now populates the
instance PK instead of leaving it `None`), so it carries `!`. The
surrounding `match tx_conn` structure is not touched (G2's PR).

### G4b — Second unnamed `connect()` silently replaces the default — `fix(ff-g)!`

`src/connection.rs:210-222`: the "already registered" check is skipped when
`is_implicit_default` — a second bare `connect(url)` silently replaces the
default engine (explicit `name="default"` already errors). **Fix:** drop the
`&& !is_implicit_default` exemption; the implicit-default case gets a
tailored message:

```
A default connection is already registered. Pass name="..." to register
an additional named connection, or call reset_engine() first to tear
down existing connections.
```

Loud error, not a warning (I-6). Tests that relied on silent replacement
within a single test function are updated (the autouse `cleanup_models`
fixture calls `reset_engine()` between tests, so only intra-test double
connects are affected). **Classification: `fix(ff-g)!`.**

### G4c — Identity-map DashMap+GIL lock-order hazard — non-breaking

The session identity map (`src/state.rs:299`) is a `DashMap` whose accessors
(`src/operations.rs:82-176`) may call into Python while holding a shard
guard (`maybe_sweep`'s `retain`, `identity_map_len`'s `iter`). The safety
invariant is **GIL-before-shard**: every identity-map access must hold the
GIL, so no thread ever blocks on a shard while another shard-holder waits
for the GIL (deadlock). Today the invariant is implicit — three accessors
(`identity_map_remove`, `identity_map_retain_model`, `identity_map_clear`)
take no `Python` token, so nothing proves it.

**Fix:** a `debug_assert_gil_held()` helper
(`debug_assert!(unsafe { pyo3::ffi::PyGILState_Check() } == 1)`) called at
the top of every `identity_map_*` accessor, plus an invariant doc comment on
the `identity_map` field in `state.rs` and on the helper. Debug builds catch
a GIL-less caller immediately; release builds pay nothing. No behavior
change, no `!`.

---

## G5 — `RustValue::into_py_any` module-handle caching

### Problem

`src/state.rs:419-461` calls `py.import("datetime"/"json"/"uuid"/"decimal")`
plus a `getattr` **per decoded value** in the hot decode path. `py.import`
hits the module cache each time; the lookups are pure overhead per
temporal/JSON/UUID/decimal column value.

### Design — `GILOnceCell` statics caching resolved callables

Module-level statics in `src/state.rs`, the canonical pyo3 pattern:

```rust
static DATETIME_FROMISOFORMAT: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
// likewise: DATE_FROMISOFORMAT, TIME_FROMISOFORMAT, JSON_LOADS,
//           UUID_CLASS, DECIMAL_CLASS
```

Each resolved once per process via
`.get_or_try_init(py, || py.import(..)?.getattr(..).map(Into::into))` and
then called directly — caching the **resolved callable** (e.g.
`datetime.datetime.fromisoformat`, `json.loads`), not the module, so the
per-value `getattr` disappears too. `into_py_any`'s match arms become
`cached.bind(py).call1((arg,))`. Ferro is abi3/single-interpreter, so
process-lifetime caches are safe.

**Measurement:** `benchmarks/` harness (`uv run --no-sync python -m
benchmarks ...`), before/after on the fetch/decode scenarios, both backends.
Baselines are effectively release-profile — sha-verify the `.so` matches the
built profile before trusting numbers. Micro win; report the delta without
over-claiming. Commit type `perf(ff-g)`.

---

## Testing summary (exit gate — written first, TDD)

| Test | Asserts |
|---|---|
| G1 slot guard | Fake class with injected slot → error naming the slot + pydantic version; real `BaseModel` passes |
| G1 setattr | `__pydantic_fields_set__` setattr failure propagates (no `let _ =`) — covered structurally by the `?` + existing hydration tests |
| G3 rollback | PG migration failing mid-plan leaves live columns unchanged |
| G4a | Non-positive PK (0 / negative) round-trips through `save()` on Postgres RETURNING path |
| G4b | Second unnamed `connect()` raises the named error; named connections + `reset_engine()` paths still work |
| G4c | `debug_assert_gil_held` present in every `identity_map_*` accessor (debug-build suite exercises it implicitly) |
| Parity | Full sqlite+postgres matrix green; shadow-strict (`FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1`) clean on the touched migrate path |

## Commit / release classification

| Change | Type |
|---|---|
| G1 guard + setattr propagation | `feat(ff-g)` |
| G3 per-table PG transaction | `feat(ff-g)` |
| G4a RETURNING PK fix | `fix(ff-g)!` |
| G4b unnamed-connect error | `fix(ff-g)!` |
| G4c GIL debug assertion + docs | `fix(ff-g)` |
| G5 handle caching | `perf(ff-g)` |

Implementation order: **G1 → G4 → G3 → G5** (per plan doc).
