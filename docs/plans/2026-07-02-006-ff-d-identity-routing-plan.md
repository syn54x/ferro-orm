# FF-D — Identity Map & Routing Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Session-scoped, memory-bounded, never-stale identity map plus single-site route resolution — Epic FF-D (D1–D5), one PR, targeting v0.13.

**Architecture:** Identity map values become Python weakrefs (instances released when user code drops them) with refresh-on-load on every fetch-hit. The global `IDENTITY_MAP` is deleted; sessionless operations run with **no** identity map (approved Option B), and the ambient default-connection path is removed (error). The `(tx_id, using, session_id)` triple threaded through every FFI call is replaced by a frozen `RouteHandle` pyclass resolved exactly once in `resolve_operation_scope`.

**Tech Stack:** PyO3 0.27.1 (abi3-py39), DashMap, pydantic v2, pytest (sqlite/postgres matrix via `just test`).

**Spec:** `docs/plans/2026-07-02-005-ff-d-identity-routing-design.md` (approved). Roadmap: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` L235–281.

## Global Constraints

- Branch: `ff-d/identity-and-routing` (exists; design doc is its first commit). Never commit to `main`.
- After **every** Rust change: `uv run maturin develop` before running Python tests.
- Quick test loop: `uv run pytest tests/<file> -x -q` (sqlite). Full matrix: `FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test` (docker `local-pg` must be up). 7 pre-existing `[postgres]` failures are tracked in #176 — not yours; compare failure lists against `main` if unsure.
- Rust tests: `cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate` and root `cargo test --no-default-features --features testing`.
- Conventional commits, scope `ff-d`, `!` on user-observable breaking commits. **No AI attribution lines of any kind** (AGENTS.md I-6) — overrides any harness default.
- **Never bulk-reformat**: `cargo fmt` / `ruff format` flag `main` itself under local toolchains. Hand-format only your own hunks (compare against `main`).
- Never panic across FFI (AGENTS.md I-3): all new Rust paths return `PyResult`, no `unwrap()` on Python-facing data.
- Docs examples: every field-declaring example shows Assignment + Annotated tabs (I-8); queries use lambda style (I-9).
- Model API is classmethods on the model (`User.get`, `User.all`, `User.create`, `user.save()`) — there is no `.objects` manager.

---

### Task 1: D5 — `Model.__init__` reads the *target* model's PK

**Files:**
- Modify: `src/ferro/models.py:213-219`
- Test: `tests/test_fk_target_pk.py` (new)

**Interfaces:**
- Consumes: `Model._primary_key_field_name()` classmethod (`src/ferro/models.py:390`) — returns the PK field name or `None`.
- Produces: no new interfaces; behavior fix only.

- [ ] **Step 1: Write the failing test**

```python
"""FF-D D5: relationship inputs must read the *target* model's PK field.

Today `Model.__init__` scans the source class's ferro_fields for the PK name
and reads that name off the target instance — correct only while every PK is
named `id`.
"""

from typing import Annotated

from ferro import Field, Model
from ferro.base import ForeignKey


def test_fk_extraction_uses_target_pk_name():
    class D5Warehouse(Model):
        code: int | None = Field(default=None, primary_key=True)
        city: str

    class D5Shipment(Model):
        id: int | None = Field(default=None, primary_key=True)
        warehouse: Annotated[D5Warehouse, ForeignKey(related_name="shipments")]

    wh = D5Warehouse(code=7, city="Reno")
    shipment = D5Shipment(warehouse=wh)
    assert shipment.warehouse_id == 7


def test_fk_extraction_with_target_pk_named_id_still_works():
    class D5Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class D5Book(Model):
        id: int | None = Field(default=None, primary_key=True)
        author: Annotated[D5Author, ForeignKey(related_name="books")]

    a = D5Author(id=3, name="x")
    b = D5Book(author=a)
    assert b.author_id == 3
```

(No `connect()` needed — this exercises pure `__init__` normalization. Class names are prefixed `D5` because the registry keys on bare class names.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_fk_target_pk.py -x -q`
Expected: first test FAILS with `assert None == 7` (source-model PK name `id` read off a target that has no `id`).

- [ ] **Step 3: Fix `Model.__init__`**

Replace the PK-scan loop in `src/ferro/models.py` (currently lines 213–219, scanning `self.__class__.ferro_fields`):

```python
                # If it's a Model instance, extract the ID
                if isinstance(val, Model):
                    # Read the *target* model's PK (FF-D D5) — the source
                    # model's PK name is irrelevant to the related instance.
                    pk_field = val.__class__._primary_key_field_name() or "id"
                    id_val = getattr(val, pk_field, None)
                    data[f"{field_name}_id"] = id_val
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_fk_target_pk.py -q` → both PASS.
Run: `uv run pytest tests/test_crud.py tests/test_relationship_engine.py -q` → no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/test_fk_target_pk.py src/ferro/models.py
git commit -m "fix(ff-d): D5 — extract FK value via the target model's PK field"
```

---

### Task 2: Weakref foundation — pinning tests + metaclass guard

**Files:**
- Modify: `src/ferro/metaclass.py` (guard in `ModelMetaclass.__new__`, `src/ferro/metaclass.py:35-40`)
- Test: `tests/test_identity_weakref.py` (new)

**Interfaces:**
- Produces: `ferro.metaclass._assert_weakref_support(cls) -> None` — raises `TypeError` if instances of `cls` cannot be weakly referenced. Called from `ModelMetaclass.__new__` on every model class. Task 3's Rust insert path is the runtime backstop.

- [ ] **Step 1: Probe the assumption (the trap check)**

Run:
```bash
uv run python -c "
import weakref
from ferro import Field, Model
class P(Model):
    id: int | None = Field(default=None, primary_key=True)
u = P(id=1)
r = weakref.ref(u)
assert r() is u
del u
import gc; gc.collect()
assert r() is None
print('pydantic v2 Model instances are weakref-able: OK')
"
```
Expected: prints OK. If this fails, STOP — the whole D1 design needs revisiting; report back before proceeding.

- [ ] **Step 2: Write the pinning tests + guard test**

`tests/test_identity_weakref.py`:

```python
"""FF-D D1: Ferro identity mapping requires weakly referenceable instances."""

import gc
import weakref

import pytest

from ferro import Field, Model
from ferro.metaclass import _assert_weakref_support


def test_model_instances_support_weakref():
    class WRUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    u = WRUser(id=1, name="a")
    r = weakref.ref(u)
    assert r() is u
    del u
    gc.collect()
    assert r() is None


def test_weakref_guard_rejects_unweakrefable_class():
    class NoWeakref:
        __slots__ = ()  # no __dict__, no __weakref__ anywhere in MRO

    with pytest.raises(TypeError, match="weak"):
        _assert_weakref_support(NoWeakref)


def test_weakref_guard_accepts_model_classes():
    class WRGuarded(Model):
        id: int | None = Field(default=None, primary_key=True)

    _assert_weakref_support(WRGuarded)  # must not raise
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_identity_weakref.py -x -q`
Expected: FAIL with `ImportError: cannot import name '_assert_weakref_support'`.

- [ ] **Step 4: Implement the guard**

In `src/ferro/metaclass.py`, add near the top (module level):

```python
def _assert_weakref_support(cls: type) -> None:
    """Reject model classes whose instances cannot be weakly referenced.

    The identity map holds weak references to instances (FF-D D1). A class
    that suppresses ``__weakref__`` would force a silent strong-ref fallback,
    which is exactly the unbounded-memory failure F3 removed — so it fails
    loudly at class definition time instead.
    """
    if not any("__weakref__" in getattr(base, "__dict__", {}) for base in cls.__mro__):
        raise TypeError(
            f"{cls.__name__} instances do not support weak references "
            "(no __weakref__ slot in the MRO). Ferro model instances must be "
            "weakly referenceable for identity mapping; remove __slots__ "
            "declarations that suppress __weakref__."
        )
```

In `ModelMetaclass.__new__`, after the class object is created (the `cls = super().__new__(...)` result) and before it is returned, add:

```python
        _assert_weakref_support(cls)
```

(Read the actual `__new__` body first; insert after class creation, before return. Do not guard `Model` itself out — the base class passes the check via pydantic's `__weakref__`.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_identity_weakref.py -q` → 3 PASS.
Run: `uv run pytest tests/test_crud.py tests/test_field_wrapper.py -q` → model creation unaffected.

- [ ] **Step 6: Commit**

```bash
git add tests/test_identity_weakref.py src/ferro/metaclass.py
git commit -m "feat(ff-d): D1 groundwork — weakref pinning tests and metaclass guard"
```

---

### Task 3: D1a — Weak-value identity map (Rust)

**Files:**
- Modify: `src/state.rs` (SessionState gets a sweep counter; global map stays *transitionally*, now holding weakrefs — deleted in Task 8)
- Modify: `src/operations.rs:106-165` (primitives), plus every primitive call site: `:853, :991, :1010, :1054, :1154, :1824, :1843, :2010, :2039, :2169, :2258`
- Modify: `src/lib.rs` (register `identity_map_len`)
- Test: `tests/test_identity_memory.py` (new)

**Interfaces:**
- Produces (Rust, private): `identity_map_get(py, session_id, key) -> PyResult<Option<Py<PyAny>>>` — returns an **upgraded strong ref** or `None` (dead entries pruned on sight); `identity_map_insert(py, session_id, key, value: &Bound<PyAny>) -> PyResult<()>` — stores a weakref, `TypeError` if the value can't be weakly referenced; `maybe_sweep(py, map, ops)`.
- Produces (FFI, test diagnostic): `_core.identity_map_len(session_id: str | None = None) -> int` — count of **live** entries.
- Rule established here: **every identity primitive takes `py: Python<'_>` first** — GIL before shard lock, always. No map access without the GIL in scope.

- [ ] **Step 1: Write the failing tests**

`tests/test_identity_memory.py`:

```python
"""FF-D D1a exit gate: the identity map is memory-bounded.

The map holds weak references: instances are released when user code drops
them; a dead entry is a miss. This is the deterministic form of the roadmap's
"bounded RSS after GC" gate — live-entry count and weakref death prove the
bound without RSS flakiness. (Strong-ref map fails both tests today.)
"""

import gc
import weakref

import pytest

from ferro import Field, Model, connect, engines
from ferro._core import identity_map_len


@pytest.mark.asyncio
async def test_dropped_instances_are_released_by_the_map(db_url):
    class MemItem(Model):
        id: int | None = Field(default=None, primary_key=True)
        payload: str

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        created = await MemItem.create(payload="x")
        pk = created.id
        ref = weakref.ref(created)

        del created
        gc.collect()
        # The map must not keep the instance alive.
        assert ref() is None

        # A dead entry is a miss: re-fetch hydrates a fresh, correct instance.
        fresh = await MemItem.get(pk)
        assert fresh.id == pk
        assert fresh.payload == "x"


@pytest.mark.asyncio
async def test_identity_map_is_bounded_under_bulk_scanning(db_url):
    class MemScan(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        await MemScan.bulk_create([MemScan(n=i) for i in range(20_000)])
        for _ in range(3):
            rows = await MemScan.all()
            assert len(rows) == 20_000
            del rows
            gc.collect()
            # Live entries collapse once user refs are gone — the map is
            # bounded by *live* instances, not by rows ever loaded.
            assert identity_map_len(s.session_id) < 100


@pytest.mark.asyncio
async def test_identity_dedup_still_works_for_live_instances(db_url):
    class MemLive(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        a = await MemLive.create(n=1)
        b = await MemLive.get(a.id)
        assert b is a  # weak map still dedupes while the instance is alive
```

(Check how existing async tests are marked — if the suite uses `asyncio_mode = auto`, drop the explicit `@pytest.mark.asyncio`; mirror `tests/test_session.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_identity_memory.py -x -q`
Expected: FAIL — first test at `assert ref() is None` (map holds strong ref today); also `ImportError` for `identity_map_len` (add the import failure note: that alone confirms red).

- [ ] **Step 3: Add sweep counters to `src/state.rs`**

Add `use std::sync::atomic::AtomicUsize;` to imports. Change:

```rust
/// Identity Map used for object tracking and deduplication.
///
/// Values are Python `weakref.ref` objects (FF-D D1) — the map never keeps an
/// instance alive. Transitional: deleted with the ambient path (FF-D D2).
pub static IDENTITY_MAP: Lazy<DashMap<(String, String, String), Py<PyAny>>> =
    Lazy::new(DashMap::new);

/// Amortized-sweep op counter for [`IDENTITY_MAP`] (see `maybe_sweep`).
pub static IDENTITY_MAP_OPS: Lazy<AtomicUsize> = Lazy::new(AtomicUsize::default);
```

In `SessionState`, add the field and initialize it in `new`:

```rust
    /// Session-local identity map; values are `weakref.ref` objects (FF-D D1).
    pub identity_map: DashMap<(String, String, String), Py<PyAny>>,
    /// Amortized-sweep op counter for `identity_map`.
    pub identity_ops: AtomicUsize,
```

- [ ] **Step 4: Rewrite the primitives in `src/operations.rs`**

Replace `identity_map_get` / `identity_map_insert` (lines 106–133) with:

```rust
/// Sweep the map for dead weakrefs every N tracked operations (FF-D D1).
///
/// Amortized O(1)/op; memory is bounded by live instances plus at most one
/// interval of tombstones. Callback-based eviction is deliberately avoided:
/// a weakref callback fires mid-GC and would re-enter the DashMap, risking a
/// shard-lock deadlock across the FFI boundary (AGENTS.md I-3).
const IDENTITY_SWEEP_INTERVAL: usize = 1024;

fn weakref_is_live(py: Python<'_>, weak: &Py<PyAny>) -> bool {
    weak.bind(py)
        .call0()
        .map(|obj| !obj.is_none())
        .unwrap_or(false)
}

fn maybe_sweep(
    py: Python<'_>,
    map: &DashMap<(String, String, String), Py<PyAny>>,
    ops: &std::sync::atomic::AtomicUsize,
) {
    use std::sync::atomic::Ordering;
    if ops.fetch_add(1, Ordering::Relaxed) % IDENTITY_SWEEP_INTERVAL
        == IDENTITY_SWEEP_INTERVAL - 1
    {
        map.retain(|_, weak| weakref_is_live(py, weak));
    }
}

fn identity_map_get(
    py: Python<'_>,
    session_id: Option<&str>,
    key: &(String, String, String),
) -> PyResult<Option<Py<PyAny>>> {
    // Clone the weakref out of the shard guard before touching Python:
    // GIL-before-shard ordering is the invariant (see maybe_sweep).
    let weak = if let Some(session_id) = session_id {
        let session = session_state(session_id)?;
        session.identity_map.get(key).map(|e| e.value().clone_ref(py))
    } else {
        IDENTITY_MAP.get(key).map(|e| e.value().clone_ref(py))
    };
    let Some(weak) = weak else { return Ok(None) };
    // Upgrade to a strong ref; hit-vs-miss is decided on the upgraded ref so
    // the instance cannot die between the check and use.
    let obj = weak.bind(py).call0()?;
    if obj.is_none() {
        // Dead entry: prune the tombstone, report a miss.
        if let Some(session_id) = session_id {
            session_state(session_id)?.identity_map.remove(key);
        } else {
            IDENTITY_MAP.remove(key);
        }
        return Ok(None);
    }
    Ok(Some(obj.unbind()))
}

fn identity_map_insert(
    py: Python<'_>,
    session_id: Option<&str>,
    key: (String, String, String),
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    // Weak by design: the map must never keep an instance alive (FF-D D1).
    // A class that can't be weakly referenced is a loud error, never a
    // silent strong-ref fallback.
    let weak = pyo3::types::PyWeakrefReference::new(value)
        .map_err(|err| {
            pyo3::exceptions::PyTypeError::new_err(format!(
                "Cannot track instance in the identity map: the class does not \
                 support weak references ({err}). Ferro model instances must be \
                 weakly referenceable."
            ))
        })?
        .into_any()
        .unbind();
    if let Some(session_id) = session_id {
        let session = session_state(session_id)?;
        session.identity_map.insert(key, weak);
        maybe_sweep(py, &session.identity_map, &session.identity_ops);
        return Ok(());
    }
    IDENTITY_MAP.insert(key, weak);
    maybe_sweep(py, &IDENTITY_MAP, &crate::state::IDENTITY_MAP_OPS);
    Ok(())
}
```

`identity_map_remove`, `identity_map_retain_model`, `identity_map_clear` keep their shapes (they don't dereference values). Add the FFI diagnostic and register it in `src/lib.rs` next to the other operation functions:

```rust
/// Count *live* identity-map entries (test diagnostic for FF-D D1).
#[pyfunction]
#[pyo3(signature = (session_id=None))]
pub fn identity_map_len(py: Python<'_>, session_id: Option<String>) -> PyResult<usize> {
    let count = |map: &DashMap<(String, String, String), Py<PyAny>>| {
        map.iter().filter(|e| weakref_is_live(py, e.value())).count()
    };
    if let Some(session_id) = session_id {
        return Ok(count(&session_state(&session_id)?.identity_map));
    }
    Ok(count(&IDENTITY_MAP))
}
```

- [ ] **Step 5: Update every primitive call site**

The compiler enumerates them; the known list — `:853` (rollback, Task 8 revisits), `:991/:1010` (fetch_all), `:1054/:1154` (fetch_one), `:1824/:1843` (fetch_where), `:2010` (register_instance), `:2039` (evict_instance), `:2169/:2258` (bulk update/delete). Pattern:
  - Sites inside `Python::attach(|py| ...)` closures: pass that `py`.
  - Sync pyfunctions (`register_instance`, `evict_instance`): add `py: Python<'_>` as first parameter (pyo3 injects it) and bind `obj` for insert: `identity_map_insert(py, session_id.as_deref(), key, obj.bind(py))?`.
  - Insert call sites that currently pass `instance.clone().unbind()` now pass `&instance` (the bound value).
  - `fetch_one`'s **sync pre-check** at `:1053-1061` keeps working for now (it has `py`); it is deleted in Task 4.

- [ ] **Step 6: Build and verify**

```bash
cargo check
uv run maturin develop
uv run pytest tests/test_identity_memory.py tests/test_identity_weakref.py -q
uv run pytest tests/test_crud.py tests/test_session.py tests/test_bulk_update.py tests/test_deletion.py tests/test_transactions.py -q
cargo test --no-default-features --features testing
```
Expected: all PASS (dedup-while-alive keeps existing identity tests green — they hold refs in local variables).

- [ ] **Step 7: Commit**

```bash
git add src/state.rs src/operations.rs src/lib.rs tests/test_identity_memory.py
git commit -m "feat(ff-d): D1a — weak-value identity map with amortized dead-entry sweep"
```

---

### Task 4: D1b — Refresh-on-load

**Files:**
- Modify: `src/hydration.rs` (extract shared field-write path; add `refresh_model_instance`)
- Modify: `src/operations.rs` — three hit sites: fetch_all (`:988-998`), fetch_one (delete sync pre-check `:1052-1061`; refresh in async body near `:1154`), fetch_where (`:1824` region)
- Test: `tests/test_identity_refresh.py` (new)

**Interfaces:**
- Consumes: `identity_map_get` from Task 3 (returns upgraded strong ref).
- Produces: `crate::hydration::refresh_model_instance(py, cls, instance, fields, py_col_names, enum_classes) -> PyResult<()>` — overwrites **every** decoded field in `instance.__dict__`, resets `__pydantic_fields_set__` to exactly the decoded columns, preserves `__ferro_connection_name`/`__ferro_persisted` and pydantic extra/private slots. Same materialization code as fresh hydration (shared `apply_decoded_fields`).

- [ ] **Step 1: Write the failing tests**

`tests/test_identity_refresh.py`:

```python
"""FF-D D1b exit gate: fetch-hits refresh the cached instance in place.

Guarantee: within a session, fetching a row you already hold returns the same
object, updated to the database's current values. The database wins over
unsaved local mutations. (Today the freshly decoded row is discarded.)
"""

import pytest

from ferro import Field, Model, connect, engines, execute


@pytest.mark.asyncio
async def test_refetch_returns_fresh_values_with_identity_preserved(db_url):
    class RefUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        score: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        u = await RefUser.create(name="old", score=1)

        # External write the ORM cache knows nothing about.
        await execute(
            "UPDATE refuser SET name = ?, score = ? WHERE id = ?", "new", 2, u.id
        )

        again = await RefUser.get(u.id)
        assert again is u          # identity preserved
        assert u.name == "new"     # FAILS today: stale "old"
        assert u.score == 2


@pytest.mark.asyncio
async def test_database_wins_over_unsaved_local_mutation(db_url):
    class RefDoc(Model):
        id: int | None = Field(default=None, primary_key=True)
        body: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        d = await RefDoc.create(body="persisted")
        d.body = "unsaved local edit"

        again = await RefDoc.get(d.id)
        assert again is d
        assert d.body == "persisted"  # documented: re-fetch overwrites


@pytest.mark.asyncio
async def test_refresh_resets_fields_set_like_fresh_hydration(db_url):
    class RefFlag(Model):
        id: int | None = Field(default=None, primary_key=True)
        a: str
        b: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        f = await RefFlag.create(a="1", b="2")
        first_fetch = await RefFlag.get(f.id)
        expected_fields_set = set(first_fetch.__pydantic_fields_set__)

        f.a = "mutated"
        again = await RefFlag.get(f.id)
        assert again is f
        assert set(f.__pydantic_fields_set__) == expected_fields_set
        assert f.a == "1"
```

(Note: Postgres uses `$1` placeholders — check how other raw-SQL tests handle the dialect; follow `tests/test_raw*.py`/`tests/test_crud.py` conventions. If raw placeholders differ per backend, mark the external-update test to build SQL per `db_backend` fixture.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_identity_refresh.py -x -q`
Expected: FAIL at `assert u.name == "new"` — cached instance returned with stale values.

- [ ] **Step 3: Extract the shared write path in `src/hydration.rs`**

Split the field loop out of `hydrate_model_instance` (its lines 100–127) into:

```rust
/// Write decoded column values into `dict` and record them in `fields_set`.
///
/// The single materialization path for both fresh hydration and fetch-hit
/// refresh (FF-D D1b) — refresh cannot drift from hydration, and a partial
/// write is structurally impossible.
fn apply_decoded_fields<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    dict: &Bound<'py, pyo3::types::PyDict>,
    fields_set: &Bound<'py, pyo3::types::PySet>,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<()> {
    for (col_name, val) in fields {
        let mut py_val = val.into_py_any(py)?;
        if let Some(enum_cls) = enum_classes.get(&col_name)
            && !py_val.is_none()
        {
            py_val = enum_cls.call1((py_val,)).map_err(|err| {
                let model = cls
                    .getattr(pyo3::intern!(py, "__name__"))
                    .and_then(|n| n.extract::<String>())
                    .unwrap_or_else(|_| "<model>".to_string());
                pyo3::exceptions::PyValueError::new_err(format!(
                    "Failed to hydrate enum field {model}.{col_name}: {err}"
                ))
            })?;
        }
        if let Some(py_name) = py_col_names.get(&col_name) {
            let py_name = py_name.bind(py);
            dict.set_item(py_name, py_val)?;
            fields_set.add(py_name)?;
        } else {
            let py_name = pyo3::types::PyString::new(py, &col_name);
            dict.set_item(&py_name, py_val)?;
            fields_set.add(&py_name)?;
        }
    }
    Ok(())
}
```

`hydrate_model_instance` calls it after setting the ferro markers; then keeps its existing `__pydantic_fields_set__` + slot initialization. Add:

```rust
/// Refresh a live cached instance from a freshly decoded row (FF-D D1b).
///
/// Overwrites every decoded field and resets `__pydantic_fields_set__` to
/// match a fresh hydration. Ferro markers (`__ferro_connection_name`,
/// `__ferro_persisted`) and pydantic extra/private slots are already present
/// on the instance and are left untouched.
pub fn refresh_model_instance<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    fields: Vec<(String, RustValue)>,
    py_col_names: &HashMap<String, pyo3::Py<pyo3::types::PyString>>,
    enum_classes: &HashMap<String, Bound<'py, PyAny>>,
) -> PyResult<()> {
    let dict_attr = instance.getattr(pyo3::intern!(py, "__dict__"))?;
    let dict = dict_attr.cast::<pyo3::types::PyDict>()?;
    let fields_set = pyo3::types::PySet::empty(py)?;
    apply_decoded_fields(py, cls, dict, &fields_set, fields, py_col_names, enum_classes)?;
    instance.setattr(pyo3::intern!(py, "__pydantic_fields_set__"), fields_set)?;
    Ok(())
}
```

- [ ] **Step 4: Rewire the three hit sites in `src/operations.rs`**

fetch_all (`:988`) — the hit arm stops discarding the row:

```rust
                if use_identity_map
                    && let Some(ref pk_val) = row_pk_val
                    && let Some(existing_obj) = identity_map_get(
                        py,
                        session_id.as_deref(),
                        &(connection_name.clone(), name.clone(), pk_val.clone()),
                    )?
                {
                    let existing = existing_obj.bind(py);
                    crate::hydration::refresh_model_instance(
                        py, cls, existing, fields, &py_col_names, &enum_classes,
                    )?;
                    results.append(existing)?;
                    continue;
                }
```

fetch_where (`:1824` region): identical transformation.

fetch_one: **delete** the pre-query sync short-circuit (`:1052-1061`) — it returns cached state without touching the database, which is exactly the stale-read class being removed. In the async body's hydration section (`:1150` region), apply the same hit-arm pattern: map hit → refresh → return the existing instance; miss → hydrate + insert as today. Add a comment at the deletion site:

```rust
    // No identity-map short-circuit before the query (FF-D D1b): the map is
    // an identity/dedup structure, not a query cache — every fetch reads the
    // database and refreshes the cached instance in place.
```

- [ ] **Step 5: Build, verify red→green, no regressions**

```bash
uv run maturin develop
uv run pytest tests/test_identity_refresh.py tests/test_identity_memory.py -q
uv run pytest tests/test_crud.py tests/test_hydration.py tests/test_hydration_equivalence.py tests/test_enum_cold_hydration.py -q
```
Expected: all PASS. If a test asserted the old fetch-one-skips-DB behavior, update it — the new semantics are the spec.

- [ ] **Step 6: Commit**

```bash
git add src/hydration.rs src/operations.rs tests/test_identity_refresh.py
git commit -m "feat(ff-d): D1b — refresh-on-load; fetch-hits update the cached instance in place"
```

---

### Task 5: Migrate test suite and docs examples to explicit sessions

Sessions work today — this migration lands **before** the Task 6 flip so every commit stays green. This is the largest mechanical chunk (~60 test files + docs pages); it is parallelizable per-file across subagents.

**Files:**
- Modify: nearly every file in `tests/` that runs ORM/raw ops after `await connect(...)` without a session
- Modify: docs pages whose examples run sessionless ops (enumerated via `tests/test_docs_examples.py`)

**Interfaces:**
- Consumes: `ferro.engines.session(name: str | None = None)` async context manager (`src/ferro/session.py:124`); `session=` kwargs on model classmethods and query entry points.
- Produces: a suite where **no test relies on ambient default-connection routing** — the invariant Task 6 enforces.

- [ ] **Step 1: Enumerate offenders**

The deprecation warning marks the exact path being removed. Run:

```bash
uv run python -c "from ferro._deprecations import enable_deprecation_warnings; enable_deprecation_warnings()" # confirm helper exists
uv run pytest tests/ -q -W "error::DeprecationWarning" 2>&1 | tail -30
```
If the ferro deprecation uses a custom category, error on that category instead (check `src/ferro/_deprecations.py` for the warning class). Capture the failing-file list — that is the migration work-list. (If warning-as-error doesn't isolate cleanly, defer enumeration to Task 6's flip and migrate against real errors — but keep the migration edits in **this** commit.)

- [ ] **Step 2: Migrate test files — the pattern**

For a typical test:

```python
# BEFORE
await connect(db_url, auto_migrate=True)
u = await User.create(name="a")

# AFTER
await connect(db_url, auto_migrate=True)
async with engines.session():
    u = await User.create(name="a")
```

Rules:
- `from ferro import engines` (already exported).
- The session block starts **after** `connect(...)`/`create_tables()`/`migrate()` (DDL entry points route by name, not by operation scope — they stay outside).
- Tests already using `transaction()` ambient blocks: wrap the transaction block inside the session (`async with engines.session():` outer, `async with transaction():` inner) — transactions inherit fine.
- Tests using **named connections + `using=`** (`tests/test_connection.py`, `tests/test_exception_mapping.py`, `tests/test_named_connections_integration.py`, `tests/test_save_semantics.py`, `tests/test_session.py`, `tests/test_transactions.py`): `using="X"` alone still works after the flip (sessionless, no identity map). Leave pure `using=` call sites as-is **unless** the test asserts `a is b` identity — identity assertions must move inside `engines.session("X")` for that connection (they lose the map otherwise; Task 8 deletes the global map they lean on).
- Identity-assertion tests (`test_crud.py:92,112`, `test_connection.py:221`, `test_bulk_update.py:39`, `test_deletion.py:67`, `test_transactions.py:143`): must run inside sessions — flag these as done explicitly.
- Do NOT add an autouse session fixture: `connect()` happens inside test bodies, and a blanket ambient session would collide with every named-connection test under D4. Explicit blocks only.

- [ ] **Step 3: Migrate docs examples**

Wrap ORM/raw operation snippets in `async with engines.session():` in every docs page whose examples run sessionless (find them via `uv run pytest tests/test_docs_examples.py -q` plus grep for `await connect` in `docs/pages/`). Keep I-8 (both declaration tabs) and I-9 (lambda predicates) intact. `docs/pages/concepts/identity-map.md` gets only the mechanical session wrapper here — its full rewrite is Task 9.

- [ ] **Step 4: Verify green on CURRENT code**

```bash
uv run pytest tests/ -q -x --db-backends=sqlite
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
```
Expected: green (modulo the 7 pre-existing #176 postgres failures).

- [ ] **Step 5: Commit**

```bash
git add tests/ docs/pages/
git commit -m "test(ff-d): migrate suite and docs examples to explicit sessions ahead of ambient-path removal"
```

---

### Task 6: D4 + ambient-default removal — the breaking flip

**Files:**
- Modify: `src/ferro/state.py:65-166` (both resolvers), `src/ferro/models.py:68-89` (`_transaction_or_using`, `_instance_transaction_route`), `src/ferro/models.py:128` (transaction), `src/ferro/raw.py:71-76`, `src/ferro/query/builder.py:112-117`
- Test: `tests/test_routing_errors.py` (new)

**Interfaces:**
- Produces: `resolve_operation_scope(*, using, session)` and `resolve_transaction_scope(*, using, session)` — `allow_legacy_default` parameter **deleted**. New errors: `RuntimeError` (no route at all), `ValueError` (explicit `using` conflicts with ambient session — D4). Return type still the triple (D3 changes it in Task 7).
- Instance ops: origin acts as the effective `using` (`using or origin` passed to the resolver), so an origin conflicting with the ambient session raises the same D4 `ValueError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_routing_errors.py`:

```python
"""FF-D D4 + ambient-default removal (v0.13, one minor ahead of the notice).

Every operation needs an explicit route: a session (ambient or session=) or
using=. The silent using-bypasses-session path is a ValueError.
"""

import pytest

from ferro import Field, Model, connect, engines, execute


@pytest.mark.asyncio
async def test_operation_with_no_route_raises(db_url):
    class RtA(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    with pytest.raises(RuntimeError, match="No database route"):
        await RtA.all()


@pytest.mark.asyncio
async def test_raw_with_no_route_raises(db_url):
    await connect(db_url, auto_migrate=True)
    with pytest.raises(RuntimeError, match="No database route"):
        await execute("SELECT 1")


@pytest.mark.asyncio
async def test_using_matching_ambient_session_is_allowed(db_url):
    class RtB(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s:
        a = await RtB.create(name="x")
        b = await RtB.all(using=s.connection_name)
        assert b[0] is a  # same connection: session-scoped, not a bypass


@pytest.mark.asyncio
async def test_using_conflicting_with_ambient_session_raises(db_url, tmp_path):
    class RtC(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    await connect(f"sqlite:{tmp_path}/other.db?mode=rwc", auto_migrate=True, name="other")
    async with engines.session():
        with pytest.raises(ValueError, match="conflicts with the ambient session"):
            await RtC.all(using="other")


@pytest.mark.asyncio
async def test_using_alone_still_works_without_identity(db_url, tmp_path):
    class RtD(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(f"sqlite:{tmp_path}/named.db?mode=rwc", auto_migrate=True, name="named")
    rows = await RtD.all(using="named")
    assert rows == []  # runs fine sessionless; no identity map involved
```

(Follow `tests/test_connection.py` for the exact multi-connection setup idiom — adjust `connect(name=...)` usage to match.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_routing_errors.py -x -q`
Expected: no-route tests FAIL (deprecation warning today, no raise); conflict test FAILS (silent bypass today).

- [ ] **Step 3: Rewrite the resolvers in `src/ferro/state.py`**

Replace `resolve_operation_scope` and `resolve_transaction_scope` bodies; delete `_LEGACY_DEFAULT_CONNECTION_REASON` and the `_deprecations` imports if now unused:

```python
_NO_ROUTE_MESSAGE = (
    "No database route for this operation. Open a session "
    "(`async with ferro.engines.session(...)`) or pass `using=`/`session=` "
    "explicitly. Implicit default-connection routing was removed in v0.13 "
    "(deprecated since v0.12); see the sessions migration guide."
)


def _using_conflict_message(using: str, session_connection: str) -> str:
    return (
        f"Explicit `using={using!r}` conflicts with the ambient session bound "
        f"to connection {session_connection!r}. Pass an explicit `session=` "
        f"for that connection, or open a session on {using!r}."
    )


def resolve_operation_scope(
    *,
    using: str | None,
    session: SessionLike | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the route for ORM/raw operations.

    Exactly one resolution site per operation (FF-D D3/D4): a session
    (ambient or explicit), an explicit `using=`, or an active transaction.
    No route is an error; `using=` conflicting with the ambient session is
    an error (the silent sessionless bypass was removed in v0.13).
    """
    tx_id = _CURRENT_TRANSACTION.get()
    tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()

    ambient_session = _CURRENT_SESSION.get()
    explicit_session = session
    effective_session = explicit_session or ambient_session

    if effective_session is not None and using is not None:
        if using != effective_session.connection_name:
            if explicit_session is not None:
                raise ValueError(
                    "Explicit `using` conflicts with explicit `session` connection"
                )
            raise ValueError(  # FF-D D4
                _using_conflict_message(using, effective_session.connection_name)
            )

    if explicit_session is None and ambient_session is not None:
        _ensure_active_session(ambient_session)
    _ensure_active_session(effective_session)

    session_id = effective_session.session_id if effective_session is not None else None

    if tx_id is not None:
        if using is not None and using != tx_connection:
            raise ValueError(
                "Operations inside a transaction inherit the transaction connection"
            )
        if effective_session is not None and tx_connection is not None:
            if effective_session.connection_name != tx_connection:
                raise ValueError(
                    "Active transaction is bound to a different connection than session"
                )
        return tx_id, None, session_id

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None:
        raise RuntimeError(_NO_ROUTE_MESSAGE)
    return None, effective_using, session_id
```

`resolve_transaction_scope`: same transformation — drop `allow_legacy_default`, same D4 conflict error, same `RuntimeError(_NO_ROUTE_MESSAGE)` when `effective_using is None` and no parent transaction.

- [ ] **Step 4: Update the four callers**

`models.py:71`, `models.py:128`, `raw.py:74`, `builder.py:115`: delete `allow_legacy_default=True` argument. In `models.py` `_instance_transaction_route` (`:76-89`): pass origin through the resolver so D4 sees it:

```python
def _instance_transaction_route(
    instance: object, using: str | None, session: "Session | None"
) -> tuple[str | None, str | None, str | None, str | None]:
    origin = _instance_origin(instance)
    if using is not None and origin is not None and using != origin:
        raise ValueError("Instance is already bound to a different connection")

    # Origin is the instance's implicit route (FF-D D4): it participates in
    # session-conflict checks instead of silently bypassing the session.
    tx_id, route_using, session_id = _transaction_or_using(using or origin, session)
    if tx_id is not None:
        tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()
        return tx_id, route_using, origin or tx_connection, session_id

    return None, route_using, route_using, session_id
```

- [ ] **Step 5: Run the full suite; fix stragglers**

```bash
uv run pytest tests/test_routing_errors.py -q          # new tests green
uv run pytest tests/ -q --db-backends=sqlite            # Task 5 migration holds
```
Any residual failures are tests still on the ambient path that Step 1's enumeration missed — migrate them now (same pattern as Task 5). Also run `uv run pytest tests/test_docs_examples.py -q`.

- [ ] **Step 6: Commit (breaking)**

```bash
git add src/ferro/state.py src/ferro/models.py src/ferro/raw.py src/ferro/query/builder.py tests/
git commit -m "feat(ff-d)!: D4 — explicit route required; using= vs ambient session is an error

Removes implicit default-connection routing (deprecated since v0.12,
announced for v0.14 — lands one minor early, accepted). Explicit using=
that conflicts with the ambient session now raises ValueError instead of
silently running sessionless. Instance origin participates in the same
conflict check."
```

---

### Task 7: D3 — One `RouteHandle`, resolved once

**Files:**
- Modify: `src/state.rs` (add `RouteHandle` pyclass), `src/lib.rs` (register class)
- Modify: `src/operations.rs` — delete `active_route_for_operation` (`:75`), `active_engine_for_connection` (`:63`), `get_transaction_route`; add `route_engine`; collapse all ~20 operation signatures (list from `grep -n "session_id=None" src/operations.rs`)
- Modify: `src/ferro/state.py` (resolvers return `RouteHandle`), `src/ferro/models.py`, `src/ferro/raw.py`, `src/ferro/query/builder.py`, `src/ferro/__init__.py` (public `evict_instance` wrapper)
- Test: `tests/test_route_single_site.py` (new)

**Interfaces:**
- Produces (Rust + FFI): frozen pyclass

  ```rust
  #[pyclass(frozen, module = "ferro._core")]
  pub struct RouteHandle {
      pub tx_id: Option<String>,
      pub connection_name: String,   // never None — a routeless handle is unrepresentable
      pub session_id: Option<String>,
  }
  ```

  with `#[new] fn new(connection_name: String, tx_id: Option<String>, session_id: Option<String>)` and `#[getter]`s for all three fields.
- Produces (Rust, private): `route_engine(route: &RouteHandle) -> PyResult<(String, Arc<EngineHandle>, Option<TransactionConnection>, Dialect)>` — the only place engine/tx-conn are derived, a cheap map lookup.
- Produces (Python): `resolve_operation_scope(*, using, session) -> RouteHandle`; `resolve_transaction_scope(*, using, session) -> RouteHandle` (its `tx_id` is the *parent* tx). `raw.Transaction` stores one `RouteHandle`.
- Every `_core` operation signature becomes `(..., route)` — `tx_id`/`using`/`session_id` parameters deleted.

- [ ] **Step 1: Write the grep-gate test**

`tests/test_route_single_site.py`:

```python
"""FF-D D3 exit gate: exactly one route-resolution site per operation.

RouteHandle may only be constructed inside src/ferro/state.py (the two
resolvers). Rust must not re-derive routes per operation.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _grep(root: Path, pattern: str, suffix: str) -> list[str]:
    hits = []
    for path in root.rglob(f"*{suffix}"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern in line and not line.lstrip().startswith(("#", "//", "///")):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}")
    return hits


def test_route_handle_constructed_only_in_state_py():
    hits = _grep(ROOT / "src" / "ferro", "RouteHandle(", ".py")
    assert hits, "expected RouteHandle construction in src/ferro/state.py"
    assert all(h.startswith("src/ferro/state.py") for h in hits), hits


def test_rust_route_rederivation_is_gone():
    assert _grep(ROOT / "src", "active_route_for_operation", ".rs") == []
    assert _grep(ROOT / "src", "active_engine_for_connection", ".rs") == []
```

Run: `uv run pytest tests/test_route_single_site.py -x -q` → FAILS (both symbols exist; no RouteHandle yet).

- [ ] **Step 2: Add `RouteHandle` + `route_engine` in Rust**

In `src/state.rs` (near the registries):

```rust
/// Opaque, immutable route for one operation (FF-D D3).
///
/// Resolved exactly once in Python's `resolve_operation_scope` and passed by
/// value through every FFI operation. `connection_name` is non-optional: the
/// removed ambient-default path is unrepresentable, not an error branch.
#[pyclass(frozen, module = "ferro._core")]
pub struct RouteHandle {
    pub tx_id: Option<String>,
    pub connection_name: String,
    pub session_id: Option<String>,
}

#[pymethods]
impl RouteHandle {
    #[new]
    #[pyo3(signature = (connection_name, tx_id=None, session_id=None))]
    fn new(
        connection_name: String,
        tx_id: Option<String>,
        session_id: Option<String>,
    ) -> Self {
        Self { tx_id, connection_name, session_id }
    }

    #[getter]
    fn connection_name(&self) -> &str {
        &self.connection_name
    }

    #[getter]
    fn tx_id(&self) -> Option<&str> {
        self.tx_id.as_deref()
    }

    #[getter]
    fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }
}
```

Register in `src/lib.rs`: `m.add_class::<state::RouteHandle>()?;`

In `src/operations.rs`, replace `active_route_for_operation` / `active_engine_for_connection` / `get_transaction_route` with:

```rust
/// Derive engine + transaction connection from an already-resolved route.
///
/// The only route-consumption site (FF-D D3): a map lookup, never a
/// re-derivation of `using`/session precedence.
fn route_engine(
    route: &crate::state::RouteHandle,
) -> PyResult<(String, Arc<EngineHandle>, Option<TransactionConnection>, Dialect)> {
    let engine = engine_for_connection(Some(route.connection_name.clone()))?;
    let tx_conn = match &route.tx_id {
        Some(tx_id) => Some(
            tx_get(route.session_id.as_deref(), tx_id)?
                .ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("Transaction not found")
                })?
                .conn,
        ),
        None => None,
    };
    let backend = engine.backend();
    Ok((route.connection_name.clone(), engine, tx_conn, backend))
}
```

(Confirm `TransactionHandle.conn` is the `TransactionConnection` field name — see `src/state.rs:195-203`.)

- [ ] **Step 3: Collapse the operation signatures**

For every `#[pyfunction]` in `operations.rs` carrying `(tx_id=None, using=None, session_id=None)` (the grep list in this plan's Task 3 references; ~20 functions: `fetch_all`, `fetch_one`, `save_record`, `update_record`, `save_bulk_records`, `fetch_where`, `fetch_where_raw`, `register_instance`, `evict_instance`, `delete_record`, `delete_where`, `update_where`, m2m add/remove/clear, `raw_execute`, `raw_fetch_all`, `raw_fetch_one`, `begin_transaction`):

```rust
// BEFORE
#[pyo3(signature = (cls, tx_id=None, using=None, session_id=None))]
pub fn fetch_all<'py>(py: Python<'py>, cls: Bound<'py, PyAny>, tx_id: Option<String>, using: Option<String>, session_id: Option<String>) -> ...

// AFTER
#[pyo3(signature = (cls, route))]
pub fn fetch_all<'py>(py: Python<'py>, cls: Bound<'py, PyAny>, route: Py<crate::state::RouteHandle>) -> ...
```

Inside each function: `let r = route.get();` (frozen + fields are `String`/`Option<String>` → `Sync`, so `.get()` works without the GIL), clone the three fields before any `future_into_py` move, and replace `active_route_for_operation(...)` calls with `route_engine(r)`. `session_id.as_deref()` for identity primitives becomes `r.session_id.as_deref()`. `begin_transaction`'s `parent_tx_id` is `r.tx_id`. `commit_transaction`, `rollback_transaction`, `transaction_connection_name` keep `(tx_id, session_id=None)` — they act on a specific transaction, not an operation route.

Build with `cargo check` and let the compiler enumerate every missed site until clean. **D3 is wide, not deep — the compiler is the checklist.**

- [ ] **Step 4: Update the Python side**

`src/ferro/state.py` — resolvers return handles (import at top: `from ._core import RouteHandle`):

```python
    # resolve_operation_scope tail (replacing the triple returns):
    if tx_id is not None:
        ...  # conflict checks unchanged
        return RouteHandle(
            connection_name=tx_connection, tx_id=tx_id, session_id=session_id
        )

    effective_using = using or (
        effective_session.connection_name if effective_session is not None else None
    )
    if effective_using is None:
        raise RuntimeError(_NO_ROUTE_MESSAGE)
    return RouteHandle(connection_name=effective_using, session_id=session_id)
```

Same for `resolve_transaction_scope` (handle's `tx_id` = parent tx). Then:
- `models.py` `_transaction_or_using` returns the handle; every call site `tx_id, using, session_id = ...` becomes `route = ...` and FFI calls pass `route`. `_instance_transaction_route` returns `(route, effective_connection)` — read `route.connection_name`/`route.tx_id` instead of the unpacked triple; the origin check logic from Task 6 is unchanged.
- `transaction()` (`models.py:128-143`): `route = resolve_transaction_scope(...)`; `tx_id = await begin_transaction(route)`; `connection_name = transaction_connection_name(tx_id, session_id=route.session_id)`; the yielded `Transaction` gets `RouteHandle(connection_name=connection_name, tx_id=tx_id, session_id=route.session_id)`.
- `raw.py`: `_transaction_or_using` returns the handle; module functions pass it; `Transaction.__slots__ = ("_route",)`, methods pass `self._route`.
- `builder.py`: `_transaction_or_using` returns the handle; all seven call sites pass it.
- `__init__.py`: `evict_instance` must stay public with a Python signature — add to `models.py`:

```python
def evict_instance(
    model_name: str,
    pk: str,
    *,
    using: str | None = None,
    session: "Session | None" = None,
) -> None:
    """Remove one instance from the active scope's identity map."""
    route = resolve_operation_scope(using=using, session=session)
    _core_evict_instance(model_name, pk, route)
```

(import `evict_instance as _core_evict_instance` from `._core`; re-export the wrapper from `__init__.py` instead of the raw FFI symbol). Update `_core.pyi` for changed signatures + `RouteHandle`.

- [ ] **Step 5: Build and verify**

```bash
uv run maturin develop
uv run pytest tests/test_route_single_site.py -q       # grep gate green
uv run pytest tests/ -q --db-backends=sqlite            # full sqlite pass
cargo test --no-default-features --features testing
```

- [ ] **Step 6: Commit**

```bash
git add src/state.rs src/operations.rs src/lib.rs src/ferro/ tests/test_route_single_site.py
git commit -m "refactor(ff-d): D3 — single RouteHandle resolved once; delete per-op route re-derivation"
```

---

### Task 8: D2 — Scoped invalidation; delete the global map

**Files:**
- Modify: `src/operations.rs` (rollback eviction `:853`; primitives lose their global arm; bulk-op retain sites `:2169/:2258` keep session-only semantics)
- Modify: `src/state.rs` (delete `IDENTITY_MAP`, `IDENTITY_MAP_OPS`)
- Modify: `src/connection.rs:9,315` and `src/migrate.rs:37,886` (delete import + `IDENTITY_MAP.clear()`)
- Test: `tests/test_identity_scoped_invalidation.py` (new)

**Interfaces:**
- Changes: all identity primitives become **session-only** — `session_id: None` arm is a no-op (`get` → `Ok(None)`, `insert`/`remove`/`retain`/`clear` → `Ok(())`). `identity_map_len(None)` returns `0`.
- Invariant produced: `grep -rn "IDENTITY_MAP" src/ crates/` is empty.

- [ ] **Step 1: Write the failing tests**

`tests/test_identity_scoped_invalidation.py`:

```python
"""FF-D D2 exit gate: invalidation is scoped, never a global clear.

Rollback evicts only the affected (connection, session); bulk update/delete
evict only (connection, model). Unrelated cached instances survive.
"""

import pytest

from ferro import Field, Model, connect, engines, transaction


@pytest.mark.asyncio
async def test_rollback_evicts_only_the_affected_session(db_url):
    class InvA(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    await connect(db_url, auto_migrate=True)
    async with engines.session() as s1:
        kept = await InvA.create(name="kept-in-s1")

    async with engines.session() as s2:
        outer = await InvA.create(name="s2-outer")
        try:
            async with transaction():
                await InvA.create(name="rolled-back")
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        # s2's map was evicted by the rollback: re-fetch hydrates fresh.
        refetched = await InvA.get(outer.id)
        assert refetched is not outer
        assert refetched.name == "s2-outer"

    # A different session was never touched (its map died with it anyway;
    # the point is the rollback path no longer clears anything global).
    async with engines.session():
        still_there = await InvA.get(kept.id)
        assert still_there.name == "kept-in-s1"


@pytest.mark.asyncio
async def test_bulk_update_evicts_only_that_model(db_url):
    class InvUser(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class InvOrder(Model):
        id: int | None = Field(default=None, primary_key=True)
        item: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        u = await InvUser.create(name="before")
        o = await InvOrder.create(item="widget")

        await InvUser.where(lambda t: t.id == u.id).update(name="after")

        fresh_u = await InvUser.get(u.id)
        assert fresh_u is not u          # evicted: fresh instance
        assert fresh_u.name == "after"

        same_o = await InvOrder.get(o.id)
        assert same_o is o               # unrelated model survived


@pytest.mark.asyncio
async def test_bulk_delete_evicts_only_that_model(db_url):
    class InvDelA(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    class InvDelB(Model):
        id: int | None = Field(default=None, primary_key=True)
        n: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        a = await InvDelA.create(n=1)
        b = await InvDelB.create(n=2)
        await InvDelA.where(lambda t: t.n == 1).delete()
        assert await InvDelA.get_or_none(a.id) is None
        assert (await InvDelB.get(b.id)) is b
```

(Check the bulk-update/delete query API against `tests/test_bulk_update.py` / `tests/test_deletion.py` and adjust `.update(...)`/`.delete()` spelling to match.)

Run: `uv run pytest tests/test_identity_scoped_invalidation.py -x -q`
Expected: rollback test FAILS today at `refetched is not outer`… **verify which arm fails**: with sessions, rollback already clears the session map, so these may pass — the red assertion for D2 is the *global* behavior, which after Task 5's migration is only observable via the grep gate. If all three pass pre-change, note that and treat the grep gate (Step 4) as the red test.

- [ ] **Step 2: Make the primitives session-only; delete the global map**

In `src/operations.rs`, every primitive's `None` arm becomes a no-op:

```rust
fn identity_map_get(
    py: Python<'_>,
    session_id: Option<&str>,
    key: &(String, String, String),
) -> PyResult<Option<Py<PyAny>>> {
    // Sessionless operations have no identity map (FF-D Option B): nothing
    // is cached, so nothing can dedup — and nothing can go stale.
    let Some(session_id) = session_id else {
        return Ok(None);
    };
    let session = session_state(session_id)?;
    let weak = session.identity_map.get(key).map(|e| e.value().clone_ref(py));
    let Some(weak) = weak else { return Ok(None) };
    let obj = weak.bind(py).call0()?;
    if obj.is_none() {
        session.identity_map.remove(key);
        return Ok(None);
    }
    Ok(Some(obj.unbind()))
}
```

Apply the same collapse to `insert` (weakref creation unchanged), `remove`, `retain_model`, `clear`, and `identity_map_len` (`None` → `Ok(0)`). Then:
- `src/state.rs`: delete `IDENTITY_MAP` + `IDENTITY_MAP_OPS` statics and the doc reference on `SessionState.identity_map`.
- `src/connection.rs`: drop `IDENTITY_MAP` from the `use` list (`:9`) and delete `IDENTITY_MAP.clear();` in `reset_engine` (`:315`) — `SESSION_REGISTRY.clear()` two lines later already tears down every session map.
- `src/migrate.rs`: drop the import (`:37`) and the clear (`:886`).
- `src/operations.rs:11`: drop `IDENTITY_MAP` from imports.
- Rollback (`:853`): the call is already `identity_map_clear(py?, session_id.as_deref())` — with the session-only primitive this *is* the scoped `(connection, session)` eviction (a session is pinned to one connection). Update the doc comment on `rollback_transaction` ("clear the identity map" → "evict the transaction's session-scoped identity map").

- [ ] **Step 3: Build and verify**

```bash
uv run maturin develop
uv run pytest tests/test_identity_scoped_invalidation.py tests/test_identity_memory.py tests/test_identity_refresh.py tests/test_routing_errors.py -q
uv run pytest tests/ -q --db-backends=sqlite
cargo test --no-default-features --features testing
```

- [ ] **Step 4: Grep gate**

```bash
grep -rn "IDENTITY_MAP" src/ crates/ && echo "FAIL: global map remains" || echo "OK: global map gone"
```
Expected: `OK`. Add this assertion to `tests/test_route_single_site.py`:

```python
def test_global_identity_map_is_gone():
    assert _grep(ROOT / "src", "IDENTITY_MAP", ".rs") == []
```

- [ ] **Step 5: Commit (breaking — sessionless caching is gone)**

```bash
git add src/ tests/
git commit -m "feat(ff-d)!: D2 — session-scoped invalidation; delete the global identity map

Sessionless operations run with no identity map (no dedup, no cache, no
stale-read surface). Rollback evicts the affected session's map only; bulk
update/delete evict per model within the session."
```

---

### Task 9: Docs — identity-map concepts rewrite + migration guide

**Files:**
- Modify: `docs/pages/concepts/identity-map.md` (rewrite sections)
- Modify: the sessions migration guide (`docs/plans/ir-first-migration-guide.md` §sessions — confirm path via `IR_FIRST_MIGRATION_GUIDE_SESSIONS` in `src/ferro/_deprecations.py`)
- Modify: `docs/plans/2026-07-02-001-fable-fixes-roadmap.md` (tick D1–D5 + exit gates L277–280)

**Interfaces:** none — documentation of the guarantees implemented in Tasks 3–8.

- [ ] **Step 1: Rewrite `docs/pages/concepts/identity-map.md`**

Keep the page structure; rewrite content to state the new guarantees **precisely**:
- *What it is*: session-scoped, weak-valued — "The identity map holds weak references: it never keeps an instance alive. When your code drops the last reference, the instance is released and the next fetch hydrates fresh."
- *The guarantee* (verbatim from the design doc): "Within a session, fetching a row you already hold returns the same object, updated in place to the database's current values. Unsaved local mutations are overwritten by a re-fetch — the database wins."
- *Scope*: "No session, no identity: operations routed with `using=` alone run without an identity map — every load returns a fresh instance, nothing is cached, nothing can go stale. Identity is a session feature because the session bounds the map's lifetime."
- *Invalidation*: "Rolling back a transaction evicts the session's map. Bulk `update()`/`delete()` evict that model's entries in the session."
- Update the existing examples to run inside `async with engines.session():` (both tabs, I-8); update the batch-jobs paragraph (weak values make manual eviction unnecessary for memory; `evict_instance` remains for forcing a fresh *instance* while holding the old one); update the stale-until-refresh caveat (re-fetch now refreshes in place; `refresh()` still works).
- The memory-bound and refresh behaviors must match `tests/test_identity_memory.py` / `tests/test_identity_refresh.py` exactly.

- [ ] **Step 2: Migration guide + roadmap ticks**

- Migration guide: add a v0.13 note — implicit default-connection routing removed (was announced for v0.14); `using=` vs ambient session now errors; sessionless `using=` ops no longer identity-mapped.
- Roadmap: tick `- [x]` for D1–D5 and the three exit-gate boxes; update the epic header line `(v0.14 cutover)` → `(landed v0.13)` and the D2/D4 body text that references the v0.14 boundary.

- [ ] **Step 3: Verify docs examples still execute**

```bash
uv run pytest tests/test_docs_examples.py tests/test_documentation_features.py -q
```

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs(ff-d): identity-map guarantees (weak values, refresh-on-load, scoped invalidation); v0.13 routing migration notes"
```

---

### Task 10: Full verification, push, PR, project tracking

**Files:** none new — verification + shipping.

- [ ] **Step 1: Full matrix + crates**

```bash
uv run maturin develop
FERRO_POSTGRES_URL=postgres://postgres:password@localhost:5432/postgres just test
cargo test -p ferro-schema-ir -p ferro-ddl-lowering -p ferro-migrate
cargo test --no-default-features --features testing
```
Expected: green modulo the 7 pre-existing `[postgres]` failures (#176) — diff the failure list against `main` to confirm no new ones.

- [ ] **Step 2: Shadow-strict rerun on touched surfaces**

```bash
FERRO_SHADOW_RUNTIME=1 FERRO_SHADOW_RUNTIME_STRICT=1 uv run pytest tests/test_crud.py tests/test_transactions.py tests/test_bulk_update.py tests/test_deletion.py tests/test_identity_memory.py tests/test_identity_refresh.py tests/test_identity_scoped_invalidation.py tests/test_routing_errors.py -q
```
(Both flags — STRICT alone is a silent no-op.)

- [ ] **Step 3: Grep gates (final)**

```bash
uv run pytest tests/test_route_single_site.py -q
grep -rn "IDENTITY_MAP" src/ crates/ | wc -l          # expect 0
grep -rn "allow_legacy_default" src/ | wc -l           # expect 0
```

- [ ] **Step 4: Formatter discipline check**

```bash
git diff main --stat
```
Confirm every touched file is intentional; no bulk reformat hunks (compare any suspicious file against `main`).

- [ ] **Step 5: Push and open PR (account `0x054`)**

Push with the inline-token URL and open the PR with the `0x054` token (osxkeychain serves the wrong credential — see memory). PR body leads, in order:
1. **Memory-bound proof** — weak-value map: dropped instances are released (`test_identity_memory.py`, deterministic form of the RSS gate; state the substitution).
2. **Stale-read-with-identity proof** — external UPDATE + re-fetch returns fresh values with `a is b` (`test_identity_refresh.py`); note `fetch_one` no longer short-circuits the DB (identity map is dedup, not a query cache).
3. **Routing collapse** — one `RouteHandle` resolved once in `resolve_operation_scope`; grep-verified single construction site; `active_route_for_operation` deleted.
4. **Scoped invalidation + global map deleted** — rollback evicts per session, bulk ops per model; `IDENTITY_MAP` static and all clear sites gone.
5. **D4** — `using=` vs ambient session is now `ValueError` (was: silent sessionless bypass).
6. **D5** — FK extraction reads the target model's PK.
7. **State plainly:** sessionless path fate (Option B: `using=`-only ops run with no identity map; no-route ops raise) and that the ambient-default removal lands in v0.13, one minor ahead of the shipped v0.14 deprecation notice — deliberate and accepted.
No AI attribution in the PR body.

- [ ] **Step 6: Project tracking (Project #7, owner `syn54x`)**

Follow the FF-C convention: tick every `[FF-D]` sub-issue checkbox, close the sub-issues, set them and the epic to Done on Project #7. (Find them: `gh issue list --repo syn54x/ferro-orm --search "FF-D" --state all`.)
