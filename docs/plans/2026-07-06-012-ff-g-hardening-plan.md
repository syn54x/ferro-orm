# FF-G Hardening Implementation Plan (G1, G3, G4, G5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hydration ABI fails loud at import if Pydantic's slots drift; a mid-run Postgres migration failure leaves the failing table's schema unchanged; three correctness edges closed (two `!` breaks); per-value `py.import` removed from the hot decode path.

**Architecture:** Four independent hardening changes per `docs/plans/2026-07-06-011-ff-g-hardening-design.md`. G1 introduces a single const slot list in `src/hydration.rs` that drives both the import-time guard and the hydrator's slot init. G3 wraps each table's Postgres migration plan in one transaction. G4 makes three surgical edits (`operations.rs` RETURNING decode, `connection.rs` duplicate-default check, identity-map GIL debug assertion). G5 caches resolved Python callables in `GILOnceCell` statics.

**Tech Stack:** Rust (pyo3, sqlx, sea-query), Python 3 (pydantic v2, pytest-asyncio), maturin, uv.

## Global Constraints

- **G2 is excluded** — do not refactor the `match tx_conn` arms or PK-discovery scans in `src/operations.rs`; G4a touches exactly the two `then_some` decode expressions.
- Branch: `ff-g/hardening`. Conventional commits, standard types only, scope `ff-g`; `!` on G4a and G4b. **No AI attribution in commits or PRs** (AGENTS.md I-6).
- **Never edit version files** (`pyproject.toml` / `Cargo.toml` versions) — semantic-release computes them.
- After every Rust change: `uv run maturin develop`. No panics across the FFI; `PyResult` everywhere; no `unwrap()` on Python-facing data (I-3).
- **Never bulk-reformat** — `cargo fmt` / `ruff format` flag `main` itself; hand-format only your own hunks.
- Tests declaring models inside functions rely on the autouse `_ferro_registry_isolation` fixture; keep function-local model names unique across a module.
- Postgres for tests: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres` (docker `local-pg`).
- Shadow-strict verification needs **both** flags: `FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1`.

---

### Task 1: G1 — Hydration ABI structural guard + swallowed-setattr fix

**Files:**
- Modify: `src/hydration.rs` (const + `init_handled_slots` replacing `set_pydantic_hydration_slots`; guard functions; module docstring at lines 1–6)
- Modify: `src/lib.rs:96-97` (call guard at `_core` init; register test pyfunction)
- Modify: `src/ferro/_core.pyi` (stub for `_verify_hydration_abi_for_test`, near the other `_for_test` entries at line 54+)
- Test: `tests/test_hydration.py`

**Interfaces:**
- Produces: `hydration::HANDLED_BASEMODEL_SLOTS: &[&str]` (pub(crate)); `hydration::verify_pydantic_slot_abi(py: Python) -> PyResult<()>`; pyfunction `_verify_hydration_abi_for_test(cls)` exposed on `ferro._core`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_hydration.py`:

```python
def test_hydration_abi_guard_names_unknown_slot():
    """FF-G G1: a BaseModel slot the hydrator does not initialize must be a
    loud, actionable error naming the slot and the pydantic version."""
    from pydantic import BaseModel

    from ferro._core import _verify_hydration_abi_for_test

    class FakeBaseModel:
        __slots__ = (*BaseModel.__slots__, "__pydantic_future_slot__")

    with pytest.raises(RuntimeError) as excinfo:
        _verify_hydration_abi_for_test(FakeBaseModel)
    message = str(excinfo.value)
    assert "__pydantic_future_slot__" in message
    assert "pydantic" in message


def test_hydration_abi_guard_passes_real_basemodel():
    """The guard that runs at ferro._core import accepts the installed
    pydantic's real BaseModel (otherwise ferro would refuse to start)."""
    from pydantic import BaseModel

    from ferro._core import _verify_hydration_abi_for_test

    _verify_hydration_abi_for_test(BaseModel)  # must not raise
```

Check the top of `tests/test_hydration.py` for existing imports (`pytest` is already imported; add none beyond what the functions import locally).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hydration.py -k abi_guard -v`
Expected: FAIL / ERROR with `ImportError: cannot import name '_verify_hydration_abi_for_test'`

- [ ] **Step 3: Implement in `src/hydration.rs`**

Add below the module docstring (and update the docstring lines 3–6 to say the authoritative list is `HANDLED_BASEMODEL_SLOTS`):

```rust
/// Every `BaseModel.__slots__` entry the zero-copy hydrator accounts for.
///
/// Single source of truth for the hydration ABI (FF-G G1): the import-time
/// guard (`verify_pydantic_slot_abi`) diffs live `BaseModel.__slots__`
/// against this list, and `init_handled_slots` iterates it to initialize
/// each slot — the guard and the hydrator cannot drift apart because they
/// read the same const. A slot pydantic adds fails the guard at import; a
/// slot added here without a matching initializer arm fails loudly on the
/// first hydrate.
pub(crate) const HANDLED_BASEMODEL_SLOTS: &[&str] = &[
    "__dict__",
    "__pydantic_fields_set__",
    "__pydantic_extra__",
    "__pydantic_private__",
];
```

Replace `set_pydantic_hydration_slots` (lines 35–57) entirely with:

```rust
/// Initialize every slot in [`HANDLED_BASEMODEL_SLOTS`] on a freshly
/// allocated instance, mirroring `BaseModel.__init__` slot assignment.
///
/// Driven by the const so the list and the initializers cannot drift: a
/// listed slot with no match arm is a loud error, never a silent skip.
fn init_handled_slots<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    fields_set: &Bound<'py, pyo3::types::PySet>,
) -> PyResult<()> {
    for slot in HANDLED_BASEMODEL_SLOTS {
        match *slot {
            // Materialized by the field writes in `apply_decoded_fields`.
            "__dict__" => {}
            "__pydantic_fields_set__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set)?;
            }
            "__pydantic_extra__" => {
                let model_config = cls.getattr(pyo3::intern!(py, "model_config"))?;
                let extra_policy = model_config.call_method1(
                    pyo3::intern!(py, "get"),
                    (pyo3::intern!(py, "extra"), pyo3::intern!(py, "ignore")),
                )?;
                let extra_slot = if extra_policy.eq(pyo3::intern!(py, "allow"))? {
                    pyo3::types::PyDict::new(py).into_any().unbind()
                } else {
                    py.None()
                };
                instance.setattr(pyo3::intern!(py, "__pydantic_extra__"), extra_slot)?;
            }
            "__pydantic_private__" => {
                instance.setattr(pyo3::intern!(py, "__pydantic_private__"), py.None())?;
            }
            other => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "HANDLED_BASEMODEL_SLOTS lists '{other}' but init_handled_slots has no \
                     initializer for it — add a match arm"
                )));
            }
        }
    }
    Ok(())
}
```

In `hydrate_model_instance`, replace lines 110–111:

```rust
    let _ = instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set);
    set_pydantic_hydration_slots(py, cls, &instance)?;
```

with (this also fixes the swallowed setattr — the `?` inside `init_handled_slots` propagates):

```rust
    init_handled_slots(py, cls, &instance, &fields_set)?;
```

Add the guard functions at the bottom of the file:

```rust
/// Diff a class's `__slots__` against [`HANDLED_BASEMODEL_SLOTS`].
///
/// # Errors
/// Returns a `PyErr` naming every unknown slot and the running pydantic
/// version when the class declares a slot the hydrator does not initialize.
fn verify_slots_handled(cls: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = cls.py();
    let slots: Vec<String> = cls.getattr(pyo3::intern!(py, "__slots__"))?.extract()?;
    let unknown: Vec<&str> = slots
        .iter()
        .map(String::as_str)
        .filter(|slot| !HANDLED_BASEMODEL_SLOTS.contains(slot))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    let version = py
        .import("pydantic")
        .and_then(|m| m.getattr("VERSION"))
        .and_then(|v| v.extract::<String>())
        .unwrap_or_else(|_| "unknown".to_string());
    Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
        "ferro's zero-copy hydration does not know how to initialize pydantic BaseModel \
         slot(s) {unknown:?} (pydantic {version}). This ferro build supports: \
         {HANDLED_BASEMODEL_SLOTS:?}. Your pydantic version is likely newer than this \
         ferro build supports — pin pydantic to a supported minor or upgrade ferro."
    )))
}

/// Import-time hydration ABI guard (FF-G G1): refuse to load `_core` when
/// pydantic's `BaseModel.__slots__` contains a slot the hydrator would leave
/// uninitialized — a loud startup error instead of silent breakage on the
/// next pydantic minor.
///
/// # Errors
/// Returns a `PyErr` if pydantic cannot be imported or declares an unknown slot.
pub(crate) fn verify_pydantic_slot_abi(py: Python<'_>) -> PyResult<()> {
    let base_model = py.import("pydantic")?.getattr("BaseModel")?;
    verify_slots_handled(&base_model)
}

/// Test-only: run the hydration ABI diff against an arbitrary class.
///
/// # Errors
/// Returns a `PyErr` when the class declares a slot the hydrator does not handle.
#[pyfunction]
pub fn _verify_hydration_abi_for_test(cls: Bound<'_, PyAny>) -> PyResult<()> {
    verify_slots_handled(&cls)
}
```

In `src/lib.rs`, first line inside `fn _core` (line 97), add:

```rust
    hydration::verify_pydantic_slot_abi(m.py())?;
```

and with the other `_for_test` registrations:

```rust
    m.add_function(wrap_pyfunction!(
        hydration::_verify_hydration_abi_for_test,
        m
    )?)?;
```

In `src/ferro/_core.pyi`, next to the other `_for_test` stubs:

```python
def _verify_hydration_abi_for_test(cls: type) -> None: ...
```

- [ ] **Step 4: Build and run tests**

Run: `uv run maturin develop && uv run pytest tests/test_hydration.py -v`
Expected: all PASS (the two new tests plus every existing hydration test — the restructured slot init must not change behavior).

- [ ] **Step 5: Sanity-check the import-time guard fires**

Run: `uv run python -c "import ferro; print('import ok')"`
Expected: `import ok` (the guard ran against real pydantic and passed).

- [ ] **Step 6: Commit**

```bash
git add src/hydration.rs src/lib.rs src/ferro/_core.pyi tests/test_hydration.py
git commit -m "feat(ff-g): hydration ABI guard — fail loud at import on unknown BaseModel slots (G1)

One shared const (HANDLED_BASEMODEL_SLOTS) drives both the import-time
guard and the hydrator's slot init, so the two cannot drift. Also
propagates the previously swallowed __pydantic_fields_set__ setattr."
```

---

### Task 2: G4a — non-positive PKs survive the RETURNING decode (`fix(ff-g)!`)

**Files:**
- Modify: `src/operations.rs:1339-1344` and `:1360-1365` (the two RETURNING decode expressions — nothing else in those `match` arms)
- Test: `tests/test_save_pk_edges.py` (new)

**Interfaces:**
- Consumes: nothing new. Produces: no API change — `save_record` still returns `int | None`; values ≤ 0 are no longer discarded.

- [ ] **Step 1: Write the failing test** — create `tests/test_save_pk_edges.py`:

```python
"""FF-G G4a: legitimate non-positive primary keys round-trip through save().

The Postgres RETURNING decode used to discard ids <= 0
(`(id > 0).then_some(id)`), leaving the instance PK None."""

from typing import Annotated, Optional

import pytest

import ferro
from ferro import Model, transaction
from ferro.base import FerroField
from ferro.raw import execute


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_sequence_generated_zero_pk_round_trips(db_url, clean_registry):
    class SeqZeroPk(Model):
        id: Annotated[Optional[int], FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        # serial PK owns sequence <table>_<col>_seq; make it emit 0 next.
        await execute('ALTER SEQUENCE "seqzeropk_id_seq" MINVALUE 0 RESTART WITH 0')
        row = SeqZeroPk(name="zero")
        await row.save()
        assert row.id == 0
        fetched = await SeqZeroPk.get(0)
        assert fetched.name == "zero"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_sequence_generated_negative_pk_round_trips_in_transaction(
    db_url, clean_registry
):
    """Covers the tx-connection RETURNING arm (the other `then_some` site)."""

    class SeqNegPk(Model):
        id: Annotated[Optional[int], FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await execute('ALTER SEQUENCE "seqnegpk_id_seq" MINVALUE -100 RESTART WITH -5')
        async with transaction():
            row = SeqNegPk(name="negative")
            await row.save()
            assert row.id == -5
        fetched = await SeqNegPk.get(-5)
        assert fetched.name == "negative"
```

Before writing, confirm the fixture/marker names against an existing postgres-only test (e.g. `tests/test_auto_migrate.py::test_postgres_type_and_nullability_reconciliation` uses `@pytest.mark.postgres_only`, `db_url`, `clean_registry`) and confirm `transaction` is importable from `ferro` (`tests/test_transactions.py` uses `async with transaction():`). Adjust imports to match reality, not this sketch.

- [ ] **Step 2: Run tests to verify they fail**

Run: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_save_pk_edges.py -v`
Expected: FAIL on `assert row.id == 0` / `assert row.id == -5` (id stays `None` — the bug).

- [ ] **Step 3: Apply the surgical fix** — in `src/operations.rs`, both arms (lines 1339–1344 and 1360–1365), replace:

```rust
                    let id = rows
                        .first()
                        .and_then(|row| row.values.first())
                        .and_then(|(_, value)| value.as_i64())
                        .unwrap_or(0);
                    Ok((id > 0).then_some(id))
```

with:

```rust
                    // FF-G G4a: return the RETURNING value as decoded. A
                    // non-positive id is a legitimate PK (sequence MINVALUE
                    // <= 0); only a missing row / non-integer PK maps to None.
                    let id = rows
                        .first()
                        .and_then(|row| row.values.first())
                        .and_then(|(_, value)| value.as_i64());
                    Ok(id)
```

Do not touch anything else in the `match tx_conn` arms (G2's PR).

- [ ] **Step 4: Build and run tests**

Run: `uv run maturin develop && FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_save_pk_edges.py tests/test_crud.py -v`
Expected: PASS (including existing CRUD tests — normal positive-id saves are unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/operations.rs tests/test_save_pk_edges.py
git commit -m "fix(ff-g)!: save() returns non-positive RETURNING PKs instead of discarding them (G4a)

BREAKING CHANGE: a Postgres RETURNING value <= 0 now populates the
instance primary key. Previously (id > 0).then_some(id) discarded it,
leaving the PK None and breaking sequences with MINVALUE <= 0."
```

---

### Task 3: G4b — second unnamed `connect()` is a loud error (`fix(ff-g)!`)

**Files:**
- Modify: `src/connection.rs:210-222` (registry check), `connect` doc comment (Errors section, ~line 179)
- Modify: `src/ferro/__init__.py:97-151` (`connect` docstring — add Raises)
- Test: `tests/test_connection.py`

**Interfaces:**
- Produces: `ValueError` with message starting `A default connection is already registered` on a second unnamed `connect()`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_connection.py` (match its existing imports/fixtures; it already imports `ferro` and uses `db_url`):

```python
@pytest.mark.asyncio
async def test_second_unnamed_connect_raises(db_url):
    """FF-G G4b: a second bare connect() must not silently replace the
    default engine."""
    await ferro.connect(db_url)
    with pytest.raises(ValueError, match="default connection is already registered"):
        await ferro.connect(db_url)


@pytest.mark.asyncio
async def test_reset_engine_then_unnamed_connect_succeeds(db_url):
    await ferro.connect(db_url)
    ferro.reset_engine()
    await ferro.connect(db_url)  # must not raise


@pytest.mark.asyncio
async def test_named_connect_after_default_still_works(db_url):
    await ferro.connect(db_url)
    await ferro.connect(db_url, name="analytics")  # must not raise
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `uv run pytest tests/test_connection.py -k "unnamed_connect or named_connect_after" -v`
Expected: `test_second_unnamed_connect_raises` FAILS (no error raised today); the other two PASS.

- [ ] **Step 3: Implement** — in `src/connection.rs`, replace the check at lines 210–222:

```rust
        if CONNECTION_REGISTRY
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
            })?
            .contains_key(&connection_name)
            && !is_implicit_default
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Connection '{}' is already registered",
                connection_name
            )));
        }
```

with:

```rust
        if CONNECTION_REGISTRY
            .read()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Failed to lock Connection Registry")
            })?
            .contains_key(&connection_name)
        {
            // FF-G G4b: a second unnamed connect() used to silently replace
            // the default engine — a loud error, not a swallow (I-6).
            return Err(pyo3::exceptions::PyValueError::new_err(
                if is_implicit_default {
                    "A default connection is already registered. Pass name=\"...\" to \
                     register an additional named connection, or call reset_engine() \
                     first to tear down existing connections."
                        .to_string()
                } else {
                    format!("Connection '{}' is already registered", connection_name)
                },
            ));
        }
```

Update the Rust `connect` doc comment's `# Errors` section and add to the Python `connect` docstring (`src/ferro/__init__.py`), after the arg list:

```
    Raises:
        ValueError: A connection with this name (or a default connection,
            when ``name`` is omitted) is already registered. Use ``name=...``
            for additional connections or ``reset_engine()`` to tear down.
```

- [ ] **Step 4: Build, run, and sweep for tests that relied on silent replacement**

Run: `uv run maturin develop && uv run pytest tests/test_connection.py -v`
Expected: PASS.

Then run the sqlite suite to find intra-test double connects:
Run: `uv run pytest tests/ -x -q`
For each failure caused by the new error: fix the test by inserting `ferro.reset_engine()` before the second `connect()` (or a `name=`), preserving the test's intent. Do not weaken the new check. Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add src/connection.rs src/ferro/__init__.py tests/
git commit -m "fix(ff-g)!: second unnamed connect() raises instead of silently replacing the default engine (G4b)

BREAKING CHANGE: connect() without a name now fails with a named error
when a default connection is already registered. Pass name=\"...\" or
call reset_engine() first."
```

---

### Task 4: G4c — identity-map GIL debug assertion

**Files:**
- Modify: `src/operations.rs` (helper + call at the top of `maybe_sweep`, `identity_map_get`, `identity_map_insert`, `identity_map_remove`, `identity_map_retain_model`, `identity_map_clear`, `identity_map_len` — lines 69–176)
- Modify: `src/state.rs:296-300` (invariant doc on the `identity_map` field)

**Interfaces:**
- Produces: `debug_assert_gil_held()` (private to `operations.rs`). No behavior change in release builds.

- [ ] **Step 1: Add the helper** — in `src/operations.rs`, above `maybe_sweep` (line 69):

```rust
/// Identity-map lock-order guard (FF-G G4c).
///
/// Every access to a session's `identity_map` DashMap must hold the GIL:
/// sweeps and liveness checks call into Python while holding a shard guard,
/// so a GIL-less thread blocking on that shard while the shard-holder waits
/// for the GIL would deadlock. GIL-before-shard is the required lock order;
/// this asserts it in debug builds (free in release).
#[inline]
fn debug_assert_gil_held() {
    debug_assert!(
        unsafe { pyo3::ffi::PyGILState_Check() } == 1,
        "identity-map access without the GIL — GIL-before-shard is the required lock order"
    );
}
```

Insert `debug_assert_gil_held();` as the first statement of: `maybe_sweep`, `identity_map_get`, `identity_map_insert`, `identity_map_remove`, `identity_map_retain_model`, `identity_map_clear`, and `identity_map_len` (7 call sites).

In `src/state.rs`, extend the doc comment on the `identity_map` field (line ~299):

```rust
    /// Session-scoped weak identity map: (connection, model, pk) → weakref.
    /// INVARIANT (FF-G G4c): access only while holding the GIL — sweeps call
    /// into Python under a shard guard, so GIL-before-shard is the required
    /// lock order (asserted by `debug_assert_gil_held` in operations.rs).
```

(Merge with whatever doc already exists on that field — keep existing text, append the invariant.)

- [ ] **Step 2: Build and run the identity-map suite under the debug build**

Run: `uv run maturin develop && uv run pytest tests/test_identity_memory.py tests/test_identity_refresh.py tests/test_identity_weakref.py tests/test_identity_scoped_invalidation.py -v`
Expected: PASS (`maturin develop` is a debug build, so every test exercises the assertion).

- [ ] **Step 3: Verify assertion coverage**

Run: `grep -c "debug_assert_gil_held();" src/operations.rs`
Expected: `7`

- [ ] **Step 4: Commit**

```bash
git add src/operations.rs src/state.rs
git commit -m "fix(ff-g): assert GIL-before-shard lock order on every identity-map access (G4c)

Documents and debug-asserts the DashMap+GIL invariant: sweeps call into
Python under a shard guard, so GIL-less access could deadlock."
```

---

### Task 5: G3 — transactional auto-migrate on Postgres

**Files:**
- Modify: `src/backend.rs` (`impl EngineConnection`, ~line 648: add `execute_sql_unprepared`)
- Modify: `src/migrate.rs` (`internal_migrate` execution loop, lines 848–863; `execute_drop_column` ~line 732: extract shared SQL/error helpers; `migrate()` doc ~line 890)
- Modify: `src/ferro/__init__.py` (`connect` docstring `migrate_updates` section) and `src/connection.rs` connect doc
- Test: `tests/test_auto_migrate.py`

**Interfaces:**
- Consumes: `EngineHandle::begin_transaction_connection()` (`src/backend.rs:631`), `EngineConnection::{commit,rollback}` (`:720,:725`).
- Produces: `EngineConnection::execute_sql_unprepared(&mut self, sql: &str) -> Result<u64, sqlx::Error>`; `render_drop_column_sql(table: &str, col: &str) -> String` and `map_drop_column_error(table: &str, col: &str, e: sqlx::Error) -> PyErr` in `migrate.rs`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_auto_migrate.py` (it already imports `ferro`, `execute`, `fetch_all`, `FerroField`; models there use `Annotated`):

```python
@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_pg_failed_migration_rolls_back_whole_table_plan(db_url, clean_registry):
    """FF-G G3: a mid-plan DDL failure on Postgres leaves the table exactly
    as it was — earlier statements of the same table's plan are rolled back.

    The differ plans all AddColumn ops before AlterColumnType ops, so the
    ADD COLUMN for `added` executes first and the USING cast on `amount`
    (varchar 'not-a-number' → integer) fails second."""

    class MigTxRollback(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        added: str | None = None
        amount: int | None = None

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute(
            'CREATE TABLE "migtxrollback" ("id" serial PRIMARY KEY, "amount" varchar)'
        )
        await execute("INSERT INTO \"migtxrollback\" (\"amount\") VALUES ('not-a-number')")

        with pytest.raises(Exception, match="Auto-migrate DDL failed"):
            await ferro.migrate()

        cols = await fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'migtxrollback'"
        )
        by_name = {c["column_name"]: c["data_type"] for c in cols}
        assert "added" not in by_name, "ADD COLUMN must be rolled back"
        assert by_name["amount"] == "character varying", "failed cast must not commit"
```

Adjust the expected exception type to the project's mapped DB error if `Exception` is too broad for house style (check how sibling tests in this file assert migrate failures, e.g. `test_migrate_updates_not_null_without_default_fails_loudly`).

- [ ] **Step 2: Run test to verify it fails**

Run: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_auto_migrate.py::test_pg_failed_migration_rolls_back_whole_table_plan -v`
Expected: FAIL on `assert "added" not in by_name` — today the ADD COLUMN commits before the cast fails.

- [ ] **Step 3: Add `EngineConnection::execute_sql_unprepared`** — in `src/backend.rs`, inside `impl EngineConnection` (next to `execute_sql`, line 649):

```rust
    /// Execute without entering the connection's prepared-statement cache —
    /// the transactional counterpart of [`EngineHandle::execute_sql_unprepared`].
    /// Migration DDL must use this: caching a statement against a schema the
    /// same migration is about to change would poison the connection.
    pub async fn execute_sql_unprepared(&mut self, sql: &str) -> Result<u64, sqlx::Error> {
        match self {
            EngineConnection::Sqlite(conn) => {
                let result = sqlx::query(sql)
                    .persistent(false)
                    .execute(&mut **conn)
                    .await?;
                Ok(result.rows_affected())
            }
            EngineConnection::Postgres(conn) => {
                let result = sqlx::query(sql)
                    .persistent(false)
                    .execute(&mut **conn)
                    .await?;
                Ok(result.rows_affected())
            }
        }
    }
```

- [ ] **Step 4: Extract shared drop-column helpers in `src/migrate.rs`** — from `execute_drop_column` (line 768–782), pull out:

```rust
fn render_drop_column_sql(table_lower: &str, col_name: &str) -> String {
    format!(
        "ALTER TABLE {} DROP COLUMN {}",
        quote_ident(table_lower),
        quote_ident(col_name)
    )
}

fn map_drop_column_error(table_lower: &str, col_name: &str, e: sqlx::Error) -> PyErr {
    crate::errors::map_db_error(
        &format!(
            "Cannot drop column '{}.{}' (columns referenced by constraints, foreign \
             keys, triggers, or views must be migrated with Alembic)",
            table_lower, col_name
        ),
        e,
    )
}
```

and have `execute_drop_column` use them (its SQLite index pre-scan stays as is).

- [ ] **Step 5: Wrap the Postgres per-table plan in a transaction** — in `internal_migrate`, replace the execution block (lines 848–863):

```rust
        for sql in &plan.statements {
            engine.execute_sql_unprepared(sql).await.map_err(|e| { ... })?;
            ddl_ran = true;
        }
        for col_name in &plan.drop_columns {
            execute_drop_column(&engine, &table_lower, col_name, backend).await?;
            ddl_ran = true;
        }
```

with:

```rust
        if backend == Dialect::Postgres {
            // FF-G G3: Postgres DDL is transactional — run this table's whole
            // plan in one transaction so a mid-plan failure leaves the table
            // exactly as it was. Per-table, not whole-run: every table ends
            // fully migrated or untouched, so a failed run is safely
            // re-runnable. (SQLite keeps statement-at-a-time execution below;
            // its scope is documented on connect()/migrate().)
            let mut conn = engine.begin_transaction_connection().await.map_err(|e| {
                crate::errors::map_db_error(
                    &format!(
                        "Auto-migrate failed to open a transaction for table '{}'",
                        table_lower
                    ),
                    e,
                )
            })?;
            let table_result: PyResult<()> = async {
                for sql in &plan.statements {
                    conn.execute_sql_unprepared(sql).await.map_err(|e| {
                        crate::errors::map_db_error(
                            &format!(
                                "Auto-migrate DDL failed for table '{}' (statement: {})",
                                table_lower, sql
                            ),
                            e,
                        )
                    })?;
                }
                for col_name in &plan.drop_columns {
                    // Postgres needs no index pre-scan (that path is
                    // SQLite-only in execute_drop_column).
                    conn.execute_sql_unprepared(&render_drop_column_sql(&table_lower, col_name))
                        .await
                        .map_err(|e| map_drop_column_error(&table_lower, col_name, e))?;
                }
                Ok(())
            }
            .await;
            match table_result {
                Ok(()) => {
                    conn.commit().await.map_err(|e| {
                        crate::errors::map_db_error(
                            &format!(
                                "Auto-migrate failed to commit DDL for table '{}'",
                                table_lower
                            ),
                            e,
                        )
                    })?;
                    ddl_ran = true;
                }
                Err(err) => {
                    if let Err(rollback_err) = conn.rollback().await {
                        crate::log_debug(format!(
                            "⚠️ Ferro Engine: rollback after failed migration of '{}' also \
                             failed: {}",
                            table_lower, rollback_err
                        ));
                    }
                    return Err(err);
                }
            }
        } else {
            for sql in &plan.statements {
                engine.execute_sql_unprepared(sql).await.map_err(|e| {
                    crate::errors::map_db_error(
                        &format!(
                            "Auto-migrate DDL failed for table '{}' (statement: {})",
                            table_lower, sql
                        ),
                        e,
                    )
                })?;
                ddl_ran = true;
            }
            for col_name in &plan.drop_columns {
                execute_drop_column(&engine, &table_lower, col_name, backend).await?;
                ddl_ran = true;
            }
        }
```

(`plan.is_empty()` already `continue`d above, so `ddl_ran = true` after commit is correct.)

- [ ] **Step 6: Document the per-backend scope.** In `src/ferro/__init__.py` `connect` docstring, `migrate_updates` section, append a bullet:

```
            - **Transactionality**: on Postgres each table's migration plan
              runs inside a single transaction — a mid-plan failure rolls the
              table back to exactly its pre-migration state. On SQLite,
              statements apply one at a time; a mid-run failure can leave
              earlier statements of that table applied (SQLite ALTERs are
              single-statement operations). For transactional multi-step
              SQLite migrations, use the Alembic bridge.
```

Mirror one-line versions in the Rust doc comments of `connect` (`src/connection.rs:165-180`) and `migrate` (`src/migrate.rs:890-898`).

- [ ] **Step 7: Build and run the migrate suites (including shadow-strict)**

Run: `uv run maturin develop && FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_auto_migrate.py tests/test_migrate_plan.py -v`
Expected: PASS, including the new rollback test.

Run: `FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_auto_migrate.py -q`
Expected: PASS (the migrate-path shadow comparator stays clean; both flags are required — STRICT alone is a no-op).

- [ ] **Step 8: Commit**

```bash
git add src/backend.rs src/migrate.rs src/connection.rs src/ferro/__init__.py tests/test_auto_migrate.py
git commit -m "feat(ff-g): run each table's Postgres migration plan in one transaction (G3)

A mid-plan DDL failure now rolls the table back to its pre-migration
state; every table ends fully migrated or untouched, so a failed run is
safely re-runnable. SQLite stays statement-at-a-time and is documented
as such on connect()/migrate()."
```

---

### Task 6: G5 — cache decode-path module handles (`perf(ff-g)`)

**Files:**
- Modify: `src/state.rs:414-462` (`RustValue::into_py_any` + new statics/helper above `impl RustValue`)
- Test: existing round-trip coverage (`tests/test_crud.py`, `tests/test_codec_plan.py`, `tests/test_hydration.py`) — no new test; correctness is unchanged decode behavior.

**Interfaces:**
- Produces: no API change. Internal: `cached_callable(py, cell, module, attrs)` in `state.rs`.

- [ ] **Step 1: Capture the BEFORE benchmark** (pre-change commit, release profile):

```bash
uv run maturin develop --release
shasum src/ferro/_core.abi3.so   # record: BEFORE sha
uv run --no-sync python -m benchmarks.run --out /tmp/ffg5-before
```

(`--no-sync` so uv does not rebuild/replace the `.so` mid-measurement; sha-verify it is the one you just built.)

- [ ] **Step 2: Implement** — in `src/state.rs`, above `impl RustValue` (line 414):

```rust
use pyo3::sync::GILOnceCell;

static DATETIME_FROMISOFORMAT: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static DATE_FROMISOFORMAT: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static TIME_FROMISOFORMAT: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static JSON_LOADS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static UUID_CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static DECIMAL_CLASS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

/// Resolve a module attribute path once per process and cache the handle
/// (FF-G G5): the hot decode path must not pay `py.import` + `getattr`
/// per value. Ferro is abi3/single-interpreter, so a process-lifetime
/// cache is safe.
///
/// # Errors
/// Returns a `PyErr` if the module import or attribute lookup fails.
fn cached_callable<'py>(
    py: Python<'py>,
    cell: &'static GILOnceCell<Py<PyAny>>,
    module: &str,
    attrs: &[&str],
) -> PyResult<&'py Bound<'py, PyAny>> {
    Ok(cell
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            let mut obj = py.import(module)?.into_any();
            for attr in attrs {
                obj = obj.getattr(*attr)?;
            }
            Ok(obj.unbind())
        })?
        .bind(py))
}
```

(put the `use` with the file's existing imports, not mid-file). Replace the six importing arms of `into_py_any`:

```rust
            RustValue::DateTime(s) => {
                cached_callable(py, &DATETIME_FROMISOFORMAT, "datetime", &["datetime", "fromisoformat"])?
                    .call1((s.replace('Z', "+00:00"),))
            }
            RustValue::Date(s) => {
                cached_callable(py, &DATE_FROMISOFORMAT, "datetime", &["date", "fromisoformat"])?
                    .call1((s,))
            }
            RustValue::Time(s) => {
                cached_callable(py, &TIME_FROMISOFORMAT, "datetime", &["time", "fromisoformat"])?
                    .call1((s,))
            }
            RustValue::Json(v) => {
                cached_callable(py, &JSON_LOADS, "json", &["loads"])?.call1((v.to_string(),))
            }
            RustValue::Uuid(s) => cached_callable(py, &UUID_CLASS, "uuid", &["UUID"])?.call1((s,)),
            RustValue::Decimal(s) => {
                cached_callable(py, &DECIMAL_CLASS, "decimal", &["Decimal"])?.call1((s,))
            }
```

`BigInt`/`Double`/`String`/`Bool`/`Blob`/`None` arms unchanged.

- [ ] **Step 3: Build (debug) and verify decode correctness**

Run: `uv run maturin develop && FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres uv run pytest tests/test_crud.py tests/test_codec_plan.py tests/test_hydration.py -q`
Expected: PASS (datetime/date/time/json/uuid/decimal round-trips unchanged).

- [ ] **Step 4: Capture the AFTER benchmark and compare**

```bash
uv run maturin develop --release
shasum src/ferro/_core.abi3.so   # record: AFTER sha (must differ from BEFORE)
uv run --no-sync python -m benchmarks.run --out /tmp/ffg5-after
uv run --no-sync python -m benchmarks.compare /tmp/ffg5-before/sqlite.json /tmp/ffg5-after/sqlite.json
uv run --no-sync python -m benchmarks.compare /tmp/ffg5-before/postgres.json /tmp/ffg5-after/postgres.json
```

Record the fetch-case deltas for the PR (the `fetch_10k_*` cases decode datetime/uuid/decimal/json per row — exactly this path). This is a micro-optimization: report the measured delta honestly, even if ~0. If stdev is a large fraction of the median, re-run with `--iters` raised before trusting the delta. **Do not commit `benchmarks/baselines/*.json` changes** (scratch dirs only). Afterwards rebuild debug for continued development: `uv run maturin develop`.

- [ ] **Step 5: Commit**

```bash
git add src/state.rs
git commit -m "perf(ff-g): cache decode-path module handles in GILOnceCell statics (G5)

RustValue::into_py_any resolved datetime/json/uuid/decimal via
py.import + getattr per decoded value; the resolved callables are now
cached once per process. Measured under benchmarks/ (see PR)."
```

---

### Task 7: Final verification + roadmap tick

**Files:**
- Modify: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (Epic FF-G, L365–418)

- [ ] **Step 1: Rust test suites**

```bash
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
cargo test --no-default-features --features testing
```
Expected: all PASS.

- [ ] **Step 2: Full matrix**

```bash
uv run maturin develop
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```
Expected: green (matrix is green on `main` — any failure is a real regression; diff against `main` in a worktree if unsure).

- [ ] **Step 3: Shadow-strict matrix (both flags)**

```bash
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```
Expected: green.

- [ ] **Step 4: Formatting check (own hunks only)** — `git diff main --stat`, then eyeball your hunks against surrounding style. Never run repo-wide `cargo fmt`/`ruff format` (they flag `main` itself).

- [ ] **Step 5: Tick the roadmap.** In `docs/plans/2026-07-02-001-fable-fixes-roadmap.md`: mark G1, G3, G4, G5 `[x]` (leave G2 `[ ]`). Split the combined exit-gate bullet into two so the done parts can be ticked truthfully:

```markdown
- [x] Slot-guard test that fails when a fake slot is injected; PG migration
      interrupted mid-plan leaves the schema unchanged.
- [ ] `cargo llvm-lines` (or LOC) shows `operations.rs` duplication removed (G2).
```

- [ ] **Step 6: Commit**

```bash
git add docs/plans/2026-07-02-001-fable-fixes-roadmap.md
git commit -m "docs(ff-g): tick G1/G3/G4/G5 in the fable-fixes roadmap (G2 ships separately)"
```

---

### Task 8: Ship — push, PR, Project #7 scaffold

- [ ] **Step 1: Verify the gh account and push with the inline token** (osxkeychain serves the wrong credential; the active account can flip mid-session):

```bash
gh api user --jq .login   # must print 0x054 — if not: gh auth switch --user 0x054
TOKEN=$(gh auth token)
git push https://0x054:$TOKEN@github.com/syn54x/ferro-orm.git ff-g/hardening
```

- [ ] **Step 2: Create milestone `FF-G`** (FF-F template: milestone #19):

```bash
gh api repos/syn54x/ferro-orm/milestones -F title="FF-G" -F description="Epic FF-G — Hardening & hygiene" --jq .number
```

- [ ] **Step 3: Create the epic issue** (template: FF-F epic #217). Body lists G1–G6 with status: G1/G3/G4/G5 in this PR, **G2 ships in its own follow-up PR**, **G6 already done (#176)**. Assign the milestone.

- [ ] **Step 4: Create sub-issues G1–G5** (five issues, milestone FF-G), then link each as a **native sub-issue** of the epic:

```bash
# per sub-issue: get its node .id, then
gh api repos/syn54x/ferro-orm/issues/<epic-number>/sub_issues -F sub_issue_id=<node .id>
```

- [ ] **Step 5: Open the PR** targeting `main`, linking the epic (`Part of #<epic>`; `Closes` the G1/G3/G4/G5 sub-issues). Lead with before/after proof:
  - slot guard: the injected-fake-slot test output (error naming the slot);
  - G3: the rollback test — mid-plan failure, `added` column absent after;
  - G4a: non-positive PK round-trip test output;
  - **state plainly the two user-observable breaks** (G4a RETURNING ≤ 0 now populates the PK; G4b second unnamed `connect()` now raises).
  - G5: measured bench delta (honest micro numbers).
  No AI attribution anywhere.

- [ ] **Step 6: Add epic + sub-issues to Project #7 and set Done.** Org is `syn54x` (GraphQL `organization(login:"syn54x")`, **not** `user`); project id `PVT_kwDOCuFwg84BbHxu`; Status field `PVTSSF_lADOCuFwg84BbHxuzhV6ko8`; Done option `98236657`. Set G1/G3/G4/G5 items **Done** (Done auto-closes them — set Done first, skip manual close). **Leave the G2 sub-issue open** (status not Done) for its own PR.
